"""Bounded parent lookup for scoped NTFS catalogue scans."""
from functools import lru_cache
from ntfs import fixup, le, record_name


def collect(volume, records, entry_type, limit, cancelled, folder=None, selected=None):
    entries, references, warnings = {}, {}, []
    total = volume.mft_size // volume.record_size
    scoped = folder is not None or selected is not None
    prefix = folder.replace("\\", "/").strip("/").casefold() if folder else None

    def parse(number, raw):
        record = fixup(raw)
        if le(record, 32, 8):
            raise ValueError("extension record")
        flags = le(record, 22, 2)
        name, reference = record_name(record)
        if not name and number != 5:
            raise ValueError("unnamed record")
        sequence = le(record, 16, 2)
        length = 0
        if not flags & 2:
            try:
                data = volume.unnamed_data(record)
                length = le(data, 48, 8) if data[8] else le(data, 16, 4)
            except ValueError:
                pass
        entry = entry_type(f"ntfs:{volume.offset}:{number}:{sequence}", name, "", not bool(flags & 1),
                           bool(flags & 2), "ntfs", volume.offset, number, sequence, length)
        return entry, (reference & ((1 << 48) - 1), reference >> 48)

    @lru_cache(maxsize=32768)
    def directory(number):
        if not 0 <= number < total:
            return None
        try:
            raw = volume.read_stream(volume.mft, number * volume.record_size, volume.record_size)
            entry, reference = parse(number, raw)
            return (entry, reference) if entry.directory else None
        except (ValueError, UnicodeError):
            return None

    def ancestry(entry, reference):
        chain = [(entry, reference)]
        seen = {entry.locator}
        while chain[-1][0].locator != 5:
            if cancelled():
                return chain, False
            number, sequence = chain[-1][1]
            if number in seen or len(chain) >= 256:
                return chain, False
            seen.add(number)
            parent = directory(number)
            if parent is None or parent[0].sequence != sequence:
                return chain, False
            chain.append(parent)
        return chain, True

    for number, raw in records("Удалённые записи NTFS"):
        try:
            # Skip active files before name/attribute parsing.
            if raw[:4] != b"FILE" or (le(raw, 22, 2) & 3) == 1:
                continue
            entry, reference = parse(number, raw)
            chain = [(entry, reference)]
            if scoped:
                # Existing unrelated directories are read lazily as parents.
                if not entry.deleted and (selected is None or entry.id not in selected):
                    continue
                chain, known = ancestry(entry, reference)
                if prefix is not None:
                    path = "/".join(e.name for e, _ in reversed(chain[:-1])).casefold() if known else ""
                    if not known or not (path == prefix or path.startswith(prefix + "/")):
                        continue
                if selected is not None and not any(e.id in selected for e, _ in chain):
                    continue
            for item, parent in chain:
                entries[item.locator] = item
                references[item.locator] = parent
            if len(entries) >= limit:
                warnings.append(f"Показан неполный список: достигнут лимит {limit:,} записей. Выберите более узкую папку для поиска.")
                break
        except (ValueError, UnicodeError):
            continue
    return entries, references, warnings
