"""Read-only NTFS volume and deleted MFT record reader for raw disk images."""

from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import NamedTuple


def le(data: bytes, offset: int, width: int, *, signed: bool = False) -> int:
    if offset < 0 or offset + width > len(data):
        raise ValueError("truncated NTFS structure")
    return int.from_bytes(data[offset:offset + width], "little", signed=signed)


class Extent(NamedTuple):
    offset: int
    size: int


@dataclass(frozen=True)
class DeletedFile:
    volume_offset: int
    record_number: int
    name: str
    size: int
    extents: tuple[Extent, ...]
    resident: bytes | None
    allocation: str
    parent_record: int = 0
    parent_sequence: int = 0
    sequence: int = 0


def partition_offsets(image, image_size: int) -> list[int]:
    """Find a volume image or NTFS partitions in conventional MBR/GPT images."""
    def read(offset, count):
        if offset < 0 or offset + count > image_size:
            return b""
        image.seek(offset)
        return image.read(count)

    candidates = {0}
    mbr = read(0, 512)
    if mbr[510:512] == b"\x55\xaa":
        for sector_size in (512, 4096):
            for slot in range(446, 510, 16):
                if mbr[slot + 4] not in (0, 0xee):
                    candidates.add(le(mbr, slot + 8, 4) * sector_size)
            gpt = read(sector_size, sector_size)
            if not gpt.startswith(b"EFI PART"):
                continue
            start = le(gpt, 72, 8) * sector_size
            entries = le(gpt, 80, 4)
            stride = le(gpt, 84, 4)
            if not (128 <= stride <= 4096):
                continue
            for index in range(min(entries, 4096)):
                entry = read(start + index * stride, stride)
                if entry and any(entry[:16]):
                    candidates.add(le(entry, 32, 8) * sector_size)
    return sorted(offset for offset in candidates if offset + 512 <= image_size)


def fixup(raw: bytes) -> bytes:
    if raw[:4] != b"FILE" or len(raw) < 512 or len(raw) % 512:
        raise ValueError("invalid MFT record signature or length")
    start, count = le(raw, 4, 2), le(raw, 6, 2)
    if start < 8 or start + count * 2 > 510 or count != len(raw) // 512 + 1:
        raise ValueError("invalid update sequence")
    marker = raw[start:start + 2]
    record = bytearray(raw)
    for index in range(1, count):
        end = index * 512
        if raw[end - 2:end] != marker:
            raise ValueError("inconsistent update sequence")
        record[end - 2:end] = raw[start + index * 2:start + index * 2 + 2]
    return bytes(record)


def attributes(record: bytes):
    cursor, end = le(record, 20, 2), le(record, 24, 4)
    if not (42 <= cursor < end <= len(record)):
        raise ValueError("invalid MFT attribute range")
    while cursor + 4 <= end:
        kind = le(record, cursor, 4)
        if kind == 0xffffffff:
            return
        size = le(record, cursor + 4, 4)
        if size < 24 or size % 8 or cursor + size > end:
            raise ValueError("invalid MFT attribute size")
        yield kind, record[cursor:cursor + size]
        cursor += size
    raise ValueError("missing MFT attribute terminator")


def resident_data(attribute: bytes) -> bytes:
    start, size = le(attribute, 20, 2), le(attribute, 16, 4)
    if attribute[8] or start < 24 or start + size > len(attribute):
        raise ValueError("invalid resident NTFS data")
    return attribute[start:start + size]


def record_name(record):
    names = []
    for kind, attr in attributes(record):
        if kind == 0x30:
            value = resident_data(attr)
            if len(value) >= 66 and 66 + value[64] * 2 <= len(value):
                names.append((value[65] == 2, value[66:66 + value[64] * 2].decode("utf-16-le"), le(value, 0, 8)))
    return min(names)[1:] if names else ("", 0)


class Volume:
    def __init__(self, image, image_size: int, offset: int):
        self.image = image
        self.offset = offset
        image.seek(offset)
        boot = image.read(512)
        if len(boot) != 512 or boot[3:11] != b"NTFS    " or boot[510:] != b"\x55\xaa":
            raise ValueError("invalid NTFS boot sector")
        sector = le(boot, 11, 2)
        csize = le(boot, 13, 1, signed=True)
        if sector not in (512, 1024, 2048, 4096) or not (-16 <= csize <= 127):
            raise ValueError("invalid NTFS geometry")
        self.cluster = sector * csize if csize >= 0 else 1 << -csize
        if self.cluster < sector or self.cluster & (self.cluster - 1):
            raise ValueError("invalid NTFS cluster size")
        declared_size = le(boot, 40, 8) * sector
        self.end = min(image_size, offset + declared_size)
        self.max_cluster = (self.end - self.offset) // self.cluster
        record_code = le(boot, 64, 1, signed=True)
        if record_code < -16:
            raise ValueError("invalid NTFS record size")
        self.record_size = (1 << -record_code) if record_code < 0 else record_code * self.cluster
        if not (512 <= self.record_size <= 65536 and self.record_size % 512 == 0):
            raise ValueError("invalid NTFS record size")

        first = offset + le(boot, 48, 8) * self.cluster
        mirror = offset + le(boot, 56, 8) * self.cluster
        for location in dict.fromkeys((first, mirror)):
            try:
                attr = self.unnamed_data(fixup(self.read(location, self.record_size)))
                initialized = le(attr, 56, 8)
                self.mft_size = min(le(attr, 48, 8), initialized)
                self.mft = self.runs(attr, self.mft_size)
                if self.mft_size < self.record_size:
                    raise ValueError("empty MFT")
                break
            except ValueError:
                continue
        else:
            raise ValueError("cannot read base MFT or MFT mirror")
        try:
            raw_bitmap = self.read_stream(self.mft, self.record_size * 6, self.record_size)
            bitmap_attr = self.unnamed_data(fixup(raw_bitmap))
            self.bitmap_size = le(bitmap_attr, 48, 8)
            if self.bitmap_size > le(bitmap_attr, 56, 8):
                raise ValueError("incomplete volume bitmap")
            self.bitmap = self.runs(bitmap_attr, self.bitmap_size)
        except ValueError:
            self.bitmap = ()
            self.bitmap_size = 0

    def read(self, offset: int, size: int) -> bytes:
        if offset < self.offset or offset + size > self.end:
            raise ValueError("NTFS extent outside source image")
        self.image.seek(offset)
        data = self.image.read(size)
        if len(data) != size:
            raise ValueError("truncated NTFS extent")
        return data

    @staticmethod
    def unnamed_data(record: bytes) -> bytes:
        data = [attr for kind, attr in attributes(record) if kind == 0x80 and not attr[9]]
        if len(data) != 1:
            raise ValueError("unsupported NTFS data layout")
        return data[0]

    def runs(self, attr: bytes, real_size: int) -> tuple[Extent, ...]:
        if attr[8] != 1 or le(attr, 12, 2) & 0xc001 or le(attr, 16, 8):
            raise ValueError("unsupported compressed, encrypted or sparse NTFS stream")
        cursor = le(attr, 32, 2)
        if cursor < 64 or cursor >= len(attr) or real_size <= 0:
            raise ValueError("invalid NTFS mapping pairs")
        cluster = 0
        mapped = 0
        extents = []
        while cursor < len(attr) and attr[cursor]:
            header = attr[cursor]
            cursor += 1
            length_bytes, delta_bytes = header & 15, header >> 4
            if not (1 <= length_bytes <= 8 and 1 <= delta_bytes <= 8):
                raise ValueError("invalid or sparse NTFS run")
            count = le(attr, cursor, length_bytes)
            cursor += length_bytes
            cluster += le(attr, cursor, delta_bytes, signed=True)
            cursor += delta_bytes
            if not count or cluster < 0 or cluster + count > self.max_cluster:
                raise ValueError("NTFS run outside source")
            mapped += count
            extents.append(Extent(self.offset + cluster * self.cluster, count * self.cluster))
        if cursor >= len(attr) or mapped != le(attr, 24, 8) + 1 or mapped * self.cluster < real_size:
            raise ValueError("incomplete NTFS runlist")
        return tuple(extents)

    def read_stream(self, extents: tuple[Extent, ...], offset: int, size: int) -> bytes:
        chunks = []
        for extent in extents:
            if offset >= extent.size:
                offset -= extent.size
                continue
            count = min(size, extent.size - offset)
            chunks.append(self.read(extent.offset + offset, count))
            size -= count
            if size == 0:
                return b"".join(chunks)
            offset = 0
        raise ValueError("NTFS stream incomplete")

    def allocation(self, extents: tuple[Extent, ...]) -> str:
        if not self.bitmap:
            return "unknown"
        for extent in extents:
            start = (extent.offset - self.offset) // self.cluster
            end = start + extent.size // self.cluster
            last_byte = (end + 7) // 8
            if last_byte > self.bitmap_size:
                return "unknown"
            try:
                bitmap = self.read_stream(self.bitmap, start // 8, last_byte - start // 8)
            except ValueError:
                return "unknown"
            bits = int.from_bytes(bitmap, "little") >> (start % 8)
            if bits & ((1 << (end - start)) - 1):
                return "allocated"
        return "free"

    def deleted(self, max_bytes: int, cancelled, on_progress):
        total = self.mft_size // self.record_size
        batch_records = max(1, (1024 * 1024) // self.record_size)
        for start in range(0, total, batch_records):
            if cancelled():
                return
            count = min(batch_records, total - start)
            batch = self.read_stream(self.mft, start * self.record_size, count * self.record_size)
            on_progress(start, total)
            for index in range(count):
                if cancelled():
                    return
                raw = batch[index * self.record_size:(index + 1) * self.record_size]
                if raw[:4] != b"FILE" or le(raw, 22, 2) & 3 or le(raw, 32, 8):
                    continue
                try:
                    record = fixup(raw)
                    name, parent = record_name(record)
                    identity = (parent & ((1 << 48) - 1), parent >> 48, le(record, 16, 2))
                    attr = self.unnamed_data(record)
                    if attr[8] == 0:
                        data = resident_data(attr)
                        if 0 <= len(data) <= max_bytes:
                            yield DeletedFile(self.offset, start + index, name, len(data), (), data, "resident", *identity)
                        continue
                    size = le(attr, 48, 8)
                    if not 0 < size <= max_bytes or size > le(attr, 56, 8):
                        continue
                    extents = self.runs(attr, size)
                    yield DeletedFile(self.offset, start + index, name, size, extents, None, self.allocation(extents), *identity)
                except (ValueError, UnicodeError):
                    continue
            on_progress(start + count, total)


def photo_extension(name: str) -> str:
    return PureWindowsPath(name).suffix.lower()
