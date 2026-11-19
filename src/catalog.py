"""Deleted metadata catalogue with verified NTFS parent sequence references."""
from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import Path

from ntfs import Volume, attributes, fixup, le, partition_offsets, record_name
from flash import FlashVolume
from sources import is_device, open_source, source_size

MAX_NODES = 500_000


@dataclass
class Entry:
    id: str
    name: str
    parent: str
    deleted: bool
    directory: bool
    method: str
    volume: int
    locator: int
    sequence: int = 0
    size: int = 0
    path: str = ""
    path_known: bool = True
    children: int = 0


@dataclass
class Catalogue:
    source: str
    entries: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    cancelled: bool = False

    def targets(self, selected):
        selected = set(selected)
        if not selected or not selected <= self.entries.keys():
            raise ValueError("Выбранные записи больше не найдены. Обновите список удалённых файлов.")
        targets = {}
        for entry in self.entries.values():
            if entry.directory or not entry.deleted:
                continue
            current, visited = entry, set()
            while current and current.id not in visited:
                if current.id in selected:
                    targets[(entry.method, entry.volume, entry.locator, entry.sequence)] = entry
                    break
                visited.add(current.id)
                current = self.entries.get(current.parent)
        return targets

    def rows(self):
        return [asdict(entry) for entry in self.entries.values()]


def _ntfs(image, size, offset, result, cancelled, progress, folder=None, selected=None):
    volume = Volume(image, size, offset)
    entries, references = {}, {}
    total = volume.mft_size // volume.record_size
    batch_count = max(1, 1024 * 1024 // volume.record_size)
    def records(stage):
        for start in range(0, total, batch_count):
            if cancelled():
                return
            count = min(batch_count, total - start)
            batch = volume.read_stream(volume.mft, start * volume.record_size, count * volume.record_size)
            progress(stage, start, total)
            for index in range(count):
                if cancelled():
                    return
                yield start + index, batch[index * volume.record_size:(index + 1) * volume.record_size]

    from ntfs_catalogue import collect
    entries, references, warnings = collect(volume, records, Entry, max(1, MAX_NODES - len(result.entries)),
                                            cancelled, folder, selected)
    result.warnings.extend(warnings)
    root = entries.get(5)
    if root is None:
        root = Entry(f"ntfs:{offset}:root", "Корень NTFS", "", False, True, "ntfs", offset, 5)
    root.name, root.parent = f"NTFS · раздел 0x{offset:X}", ""
    lost = Entry(f"ntfs:{offset}:lost", "Путь утрачен", root.id, False, True, "ntfs", offset, -1, path_known=False)
    result.entries[root.id] = root
    result.entries[lost.id] = lost
    for number, entry in entries.items():
        if number == 5:
            continue
        parent, sequence = references[number]
        found = entries.get(parent)
        if parent == 5 and found is None and sequence == 0:
            entry.parent = root.id
        elif found and found.directory and found.sequence == sequence and found.id != entry.id:
            entry.parent = found.id
        else:
            entry.parent = lost.id
            entry.path_known = False
        result.entries[entry.id] = entry


def _flash(image, size, offset, boot, result, cancelled, progress):
    volume = FlashVolume(image, size, offset, boot)
    root_id = f"{volume.kind}:{offset}:root"
    result.entries[root_id] = Entry(root_id, f"{volume.kind.upper()} · раздел 0x{offset:X}", "", False, True,
                                    volume.kind, offset, volume.root)
    queue = [(volume.root, 0, False, root_id, False)]
    visited = set()
    for cluster, length, no_chain, parent, inherited in queue:
        if cancelled():
            return
        if cluster in visited or not 2 <= cluster < volume.count + 2:
            continue
        visited.add(cluster)
        progress("Список " + volume.kind.upper(), len(visited), len(queue))
        for record in volume.directory_records(cluster, length, no_chain, cancelled):
            name, deleted, directory, start, length, contiguous, locator = record
            if name in {".", ".."} or not name:
                continue
            deleted = deleted or inherited
            if not directory and not deleted:
                continue
            fingerprint = hashlib.sha256((name + f"\0{length}\0{start}\0{directory}").encode("utf-8")).hexdigest()[:16]
            ident = f"{volume.kind}:{offset}:{locator}:{start}:{fingerprint}"
            entry = Entry(ident, name, parent, deleted, directory, volume.kind, offset, locator, start, length)
            result.entries[ident] = entry
            if len(result.entries) >= MAX_NODES:
                raise OSError("Слишком много записей каталогов. Выберите отдельный раздел.")
            if directory:
                # Cleared FAT chains cannot reveal the old directory length. A
                # deleted FAT32 directory therefore exposes its first cluster only.
                read_length = length if volume.kind == "exfat" else volume.cluster_size if deleted else 0
                queue.append((start, read_length, contiguous or deleted, ident, deleted))


def build_catalogue(source, cancelled=lambda: False, progress=lambda *_: None, *, folder=None, selected=None):
    source = str(source) if is_device(source) else str(Path(source).resolve(strict=True))
    result = Catalogue(source)
    with open_source(source) as image:
        size = source_size(image)
        recognized = False
        for offset in partition_offsets(image, size):
            if cancelled():
                break
            image.seek(offset)
            boot = image.read(512)
            try:
                if boot[3:11] == b"NTFS    ":
                    recognized = True
                    _ntfs(image, size, offset, result, cancelled, progress, folder, selected)
                elif boot[3:11] == b"EXFAT   " or boot[82:90] == b"FAT32   ":
                    recognized = True
                    _flash(image, size, offset, boot, result, cancelled, progress)
            except (ValueError, OSError) as error:
                result.warnings.append(f"Раздел 0x{offset:X}: {error}")
        if not recognized and not cancelled():
            result.warnings.append("Не найдены записи NTFS, FAT32 или exFAT. Попробуйте выбрать раздел с буквой вместо физического диска. Зашифрованный том должен быть разблокирован в Windows.")
    # Resolve paths iteratively; cycles and missing ancestors remain explicit.
    for entry in result.entries.values():
        parts, current, seen = [], entry, set()
        while current.parent and len(seen) < 256 and current.id not in seen:
            seen.add(current.id)
            parts.append(current.name)
            parent = result.entries.get(current.parent)
            if parent is None:
                entry.path_known = False
                break
            entry.path_known &= parent.path_known
            current = parent
        if current.id in seen or len(seen) >= 256:
            entry.parent = ""
            entry.path_known = False
        entry.path = "/".join(reversed(parts)) or entry.name
    for entry in result.entries.values():
        if not entry.directory:
            current, seen = result.entries.get(entry.parent), set()
            while current and current.id not in seen:
                seen.add(current.id)
                current.children += 1
                current = result.entries.get(current.parent)
    result.cancelled = cancelled()
    return result
