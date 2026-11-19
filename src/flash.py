"""Read-only FAT32 / exFAT directory recovery for camera cards and USB drives."""

import math
from dataclasses import dataclass
from functools import lru_cache

from formats import PHOTO_EXTENSIONS
from ntfs import Extent, le, photo_extension

FAT32_EOC = 0x0ffffff8
EXFAT_EOC = 0xfffffff8


@dataclass(frozen=True)
class DirectoryFile:
    name: str
    size: int
    extents: tuple[Extent, ...]
    allocation: str
    mapping: str
    directory_offset: int
    sequence: int = 0


class FlashVolume:
    def __init__(self, image, size, offset, boot):
        self.image = image
        self.offset = offset
        self.end = size
        self.kind = "exfat" if boot[3:11] == b"EXFAT   " else "fat32"
        self.bitmap = ()
        self.bitmap_length = 0
        self.directory_clusters = set()
        if self.kind == "exfat":
            bps, spc = boot[108:110]
            if not 9 <= bps <= 12 or spc > 25 - bps or boot[110] not in (1, 2):
                raise ValueError("invalid exFAT geometry")
            sector = 1 << bps
            self.cluster_size = sector << spc
            self.count = le(boot, 92, 4)
            self.root = le(boot, 96, 4)
            fat_length = le(boot, 84, 4)
            self.active_fat = le(boot, 106, 2) & 1 if boot[110] == 2 else 0
            fat = le(boot, 80, 4) + self.active_fat * fat_length
            self.end = min(size, offset + le(boot, 72, 8) * sector)
            self.heap = offset + le(boot, 88, 4) * sector
            self.eoc = EXFAT_EOC
            self.mask = 0xffffffff
        else:
            sector = le(boot, 11, 2)
            sectors_per_cluster = boot[13]
            if sector not in (512, 1024, 2048, 4096) or sectors_per_cluster not in (1, 2, 4, 8, 16, 32, 64, 128):
                raise ValueError("invalid FAT32 geometry")
            self.cluster_size = sector * sectors_per_cluster
            fat_count = boot[16]
            reserved = le(boot, 14, 2)
            fat_length = le(boot, 36, 4)
            total_sectors = le(boot, 19, 2) or le(boot, 32, 4)
            heap_sector = reserved + fat_count * fat_length
            flags = le(boot, 40, 2)
            active = (flags & 15) if flags & 128 else 0
            if not reserved or fat_count not in (1, 2) or active >= fat_count or le(boot, 17, 2) or le(boot, 22, 2):
                raise ValueError("invalid FAT32 layout")
            fat = reserved + active * fat_length
            self.heap = offset + heap_sector * sector
            self.count = (total_sectors - heap_sector) // sectors_per_cluster
            self.end = min(size, offset + total_sectors * sector)
            self.root = le(boot, 44, 4) & 0x0fffffff
            self.eoc = FAT32_EOC
            self.mask = 0x0fffffff
        self.fat_offset = offset + fat * sector
        self.fat_length = fat_length * sector
        if (not 2 <= self.root < self.count + 2 or self.heap >= self.end
                or not offset < self.fat_offset < self.heap or self.fat_offset + self.fat_length > self.heap
                or (self.count + 2) * 4 > self.fat_length):
            raise ValueError("invalid flash volume layout")
        # Scope the cache to this volume and avoid retaining handles after a scan.
        self.fat_block = lru_cache(maxsize=64)(self.fat_block)

    def read(self, offset, size):
        if offset < self.offset or offset + size > self.end:
            raise ValueError("flash extent outside source image")
        self.image.seek(offset)
        data = self.image.read(size)
        if len(data) != size:
            raise ValueError("truncated flash extent")
        return data

    def fat_block(self, page):
        offset = page * 4096
        return self.read(self.fat_offset + offset, min(4096, self.fat_length - offset))

    def fat(self, cluster):
        page, offset = divmod(cluster * 4, 4096)
        return le(self.fat_block(page), offset, 4) & self.mask

    def chain(self, start, limit, cancelled):
        seen = set()
        while len(seen) < limit and 2 <= start < self.count + 2 and start not in seen:
            if cancelled():
                return
            seen.add(start)
            yield start
            start = self.fat(start)
            if start >= self.eoc:
                return

    def extents(self, clusters):
        result = []
        for cluster in clusters:
            offset = self.heap + (cluster - 2) * self.cluster_size
            if offset + self.cluster_size > self.end:
                raise ValueError("cluster outside source image")
            if result and result[-1].offset + result[-1].size == offset:
                previous = result.pop()
                result.append(Extent(previous.offset, previous.size + self.cluster_size))
            else:
                result.append(Extent(offset, self.cluster_size))
        return tuple(result)

    def read_stream(self, extents, start, length):
        chunks = []
        for extent in extents:
            if start >= extent.size:
                start -= extent.size
                continue
            take = min(length, extent.size - start)
            chunks.append(self.read(extent.offset + start, take))
            length -= take
            if not length:
                return b"".join(chunks)
            start = 0
        raise ValueError("incomplete directory stream")

    def allocation(self, clusters):
        if self.kind == "fat32":
            return "allocated" if any(self.fat(cluster) for cluster in clusters) else "free"
        if not self.bitmap:
            return "unknown"
        last_page = -1
        block = b""
        for cluster in clusters:
            index = cluster - 2
            page, bit = divmod(index, 4096 * 8)
            if page != last_page:
                start = page * 4096
                length = min(4096, self.bitmap_length - start)
                if length <= 0:
                    return "unknown"
                block = self.read_stream(self.bitmap, start, length)
                last_page = page
            if bit // 8 >= len(block):
                return "unknown"
            if block[bit // 8] & (1 << (bit % 8)):
                return "allocated"
        return "free"

    def candidate(self, name, size, start, no_chain, offset, maximum, cancelled):
        if size == 0 and start == 0:
            yield DirectoryFile(name, 0, (), "unknown", "empty_declared", offset, 0)
            return
        if not 0 < size <= maximum or not 2 <= start < self.count + 2:
            return
        length = math.ceil(size / self.cluster_size)
        if not no_chain:
            mapped = list(self.chain(start, length, cancelled))
            if len(mapped) == length and self.fat(mapped[-1]) >= self.eoc:
                yield DirectoryFile(name, size, self.extents(mapped), self.allocation(mapped),
                                    "fat_chain", offset, start)
                if mapped == list(range(start, start + length)):
                    return
        if cancelled() or start + length > self.count + 2:
            return
        contiguous = range(start, start + length)
        yield DirectoryFile(name, size, self.extents(contiguous), self.allocation(contiguous),
                            "contiguous_declared" if no_chain else "contiguous_guess", offset, start)

    def entries(self, root, length, no_chain, cancelled):
        limit = 32 * 1024 * 1024 // self.cluster_size
        if length:
            limit = min(limit, math.ceil(length / self.cluster_size))
        if no_chain:
            clusters = range(root, min(root + limit, self.count + 2))
        else:
            clusters = self.chain(root, limit, cancelled)
        for cluster in clusters:
            if cancelled() or cluster in self.directory_clusters:
                return
            self.directory_clusters.add(cluster)
            physical = self.heap + (cluster - 2) * self.cluster_size
            count = min(self.cluster_size, length) if length else self.cluster_size
            data = self.read(physical, count)
            for cursor in range(0, len(data) - 31, 32):
                entry = data[cursor:cursor + 32]
                if not entry[0]:
                    return
                yield physical + cursor, entry
            if length:
                length -= count
                if length == 0:
                    return

    def directory_records(self, root, length, no_chain, cancelled):
        entries = iter(self.entries(root, length, no_chain, cancelled))
        if self.kind == "fat32":
            for offset, entry in entries:
                if cancelled():
                    return
                flags = entry[11]
                if flags & 8:
                    continue
                deleted = entry[0] == 0xe5
                base = (("_" if deleted else entry[:1].decode("cp437")) + entry[1:8].decode("cp437")).rstrip(" ")
                extension = entry[8:11].decode("cp437").rstrip(" ")
                name = base + ("." + extension if extension else "")
                start = ((le(entry, 20, 2) << 16) | le(entry, 26, 2)) & 0x0fffffff
                yield name, deleted, bool(flags & 16), start, le(entry, 28, 4), False, offset
            return
        pending = None
        while not cancelled():
            found = pending or next(entries, None)
            pending = None
            if found is None:
                return
            offset, first = found
            if first[0] not in (0x05, 0x85) or not 2 <= first[1] <= 19:
                continue
            active = first[0] == 0x85
            records = [first]
            for index in range(first[1]):
                found = next(entries, None)
                if found is None:
                    return
                expected = (0x40 if index == 0 else 0x41) | (0x80 if active else 0)
                if found[1][0] != expected:
                    pending = found
                    break
                records.append(found[1])
            if len(records) != first[1] + 1:
                continue
            checksum = 0
            for number, entry in enumerate(records):
                for index, value in enumerate(entry):
                    if number == 0 and index in (2, 3):
                        continue
                    value = value | 0x80 if index == 0 else value
                    checksum = (((checksum >> 1) | (checksum << 15)) + value) & 0xffff
            if checksum != le(first, 2, 2):
                continue
            stream = records[1]
            name_length = stream[3]
            name_bytes = b"".join(record[2:] for record in records[2:])
            if not 0 < name_length * 2 <= len(name_bytes):
                continue
            name = name_bytes[:name_length * 2].decode("utf-16-le", errors="replace")
            size = le(stream, 24, 8)
            if not le(first, 4, 2) & 16 and le(stream, 8, 8) != size:
                continue
            yield name, not active, bool(le(first, 4, 2) & 16), le(stream, 20, 4), size, bool(stream[1] & 2), offset

    def deleted(self, max_bytes, cancelled, on_progress):
        queue = [(self.root, 0, False, False)]
        visited = set()
        for root, length, no_chain, inherited in queue:
            if cancelled() or len(visited) >= 100_000:
                return
            if root in visited or not 2 <= root < self.count + 2:
                continue
            visited.add(root)
            on_progress(len(visited), len(queue))
            if self.kind == "exfat" and root == self.root:
                for _, entry in self.entries(root, length, no_chain, cancelled):
                    if entry[0] == 0x81 and (entry[1] & 1) == self.active_fat:
                        start, size = le(entry, 20, 4), le(entry, 24, 8)
                        if (self.count + 7) // 8 <= size <= 512 * 1024 * 1024:
                            need = math.ceil(size / self.cluster_size)
                            clusters = list(self.chain(start, need, cancelled))
                            if len(clusters) == need:
                                self.bitmap = self.extents(clusters)
                                self.bitmap_length = size
                        break
                self.directory_clusters.clear()
            for name, deleted, directory, start, size, contiguous, offset in self.directory_records(root, length, no_chain, cancelled):
                deleted = deleted or inherited
                if directory:
                    if name not in {".", ".."}:
                        read_length = size if self.kind == "exfat" else self.cluster_size if deleted else 0
                        queue.append((start, read_length, contiguous or deleted, deleted))
                elif deleted:
                    yield from self.candidate(name, size, start, contiguous, offset, max_bytes, cancelled)

    def fat_entries(self, entries, queue, maximum, cancelled):
        for offset, entry in entries:
            flags = entry[11]
            if flags & 8:
                continue
            deleted = entry[0] == 0xe5
            start = ((le(entry, 20, 2) << 16) | le(entry, 26, 2)) & 0x0fffffff
            if flags & 16:
                if not deleted and entry[0] != 46:
                    queue.append((start, 0, False))
                continue
            if not deleted:
                continue
            base = ("_" + entry[1:8].decode("cp437")).rstrip(" ")
            extension = entry[8:11].decode("cp437").rstrip(" ")
            name = base + ("." + extension if extension else "")
            if photo_extension(name) in PHOTO_EXTENSIONS:
                yield from self.candidate(name, le(entry, 28, 4), start, False, offset, maximum, cancelled)

    def exfat_entries(self, entries, queue, maximum, cancelled):
        pending = None
        while not cancelled():
            found = pending or next(entries, None)
            pending = None
            if found is None:
                return
            offset, first = found
            if first[0] not in (0x05, 0x85) or not 2 <= first[1] <= 19:
                continue
            active = first[0] == 0x85
            records = [first]
            for index in range(first[1]):
                found = next(entries, None)
                if found is None:
                    return
                expected = (0x40 if index == 0 else 0x41) | (0x80 if active else 0)
                if found[1][0] != expected:
                    pending = found
                    break
                records.append(found[1])
            if len(records) != first[1] + 1:
                continue
            checksum = 0
            for record_number, entry in enumerate(records):
                for index, value in enumerate(entry):
                    if record_number == 0 and index in (2, 3):
                        continue
                    value = value | 0x80 if index == 0 else value
                    checksum = (((checksum >> 1) | (checksum << 15)) + value) & 0xffff
            if checksum != le(first, 2, 2):
                continue
            stream = records[1]
            no_chain = bool(stream[1] & 2)
            name_len = stream[3]
            name_bytes = b"".join(entry[2:] for entry in records[2:])
            if not 0 < name_len * 2 <= len(name_bytes):
                continue
            name = name_bytes[:name_len * 2].decode("utf-16-le", errors="replace")
            start, size = le(stream, 20, 4), le(stream, 24, 8)
            if le(first, 4, 2) & 16:
                if active and size:
                    queue.append((start, size, no_chain))
                continue
            if not active and photo_extension(name) in PHOTO_EXTENSIONS and le(stream, 8, 8) == size:
                yield from self.candidate(name, size, start, no_chain, offset, maximum, cancelled)
