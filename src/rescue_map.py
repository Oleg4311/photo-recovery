"""Read image coordinates using only finished blocks in a GNU ddrescue map."""

from bisect import bisect_right
from pathlib import Path

STATUS = {"?", "*", "/", "-", "+"}
MAX_MAP_BYTES = 64 * 1024 * 1024


class RescueMap:
    def __init__(self, trusted, image_size):
        self.trusted = tuple(trusted)
        self.starts = tuple(start for start, _ in trusted)
        self.mapped_bytes = sum(end - start for start, end in trusted)
        self.size = image_size

    @classmethod
    def load(cls, path: Path, image_size: int, output_delta: int = 0):
        trusted = []
        previous_end = 0
        has_status = False
        blocks = 0
        with path.open("rb") as stream:
            remaining = MAX_MAP_BYTES
            while line := stream.readline(4097):
                if len(line) > 4096:
                    raise ValueError("Строка в карте ddrescue превышает 4096 байт.")
                remaining -= len(line)
                if remaining < 0:
                    raise ValueError("Карта ddrescue больше 64 МиБ.")
                try:
                    fields = line.decode("ascii").split("#", 1)[0].split()
                    if not fields:
                        continue
                    if not has_status:
                        if len(fields) not in (2, 3) or fields[1] not in STATUS | {"F", "G"}:
                            raise ValueError
                        int(fields[0], 0)
                        if len(fields) == 3:
                            int(fields[2], 0)
                        has_status = True
                        continue
                    if len(fields) != 3 or fields[2] not in STATUS:
                        raise ValueError
                    start, size = (int(field, 0) for field in fields[:2])
                    if start < previous_end or size <= 0:
                        raise ValueError
                    previous_end = start + size
                    blocks += 1
                    if fields[2] != "+":
                        continue
                    start, end = max(0, start + output_delta), min(image_size, previous_end + output_delta)
                    if start >= end:
                        continue
                    if trusted and trusted[-1][1] == start:
                        trusted[-1] = (trusted[-1][0], end)
                    else:
                        trusted.append((start, end))
                except (UnicodeError, ValueError) as error:
                    raise ValueError("Неверный формат или пересекающиеся блоки в карте ddrescue.") from error
        if not has_status or not blocks:
            raise ValueError("В карте ddrescue нет блоков.")
        return cls(trusted, image_size)

    def complete(self, offset, size):
        if not size:
            return True
        index = bisect_right(self.starts, offset) - 1
        return index >= 0 and offset + size <= self.trusted[index][1]

    def ranges(self, start, end):
        index = max(0, bisect_right(self.starts, start) - 1)
        while index < len(self.trusted):
            low, high = self.trusted[index]
            if low >= end:
                return
            if high > start:
                yield max(low, start), min(high, end)
            index += 1


class MappedImage:
    """Mask unrescued bytes, which can contain zeros or stale destination bytes."""

    def __init__(self, handle, mapping):
        self.handle = handle
        self.mapping = mapping

    def seek(self, position):
        return self.handle.seek(position)

    def read(self, count):
        start = self.handle.tell()
        end = min(start + count, self.mapping.size)
        if start >= end:
            return b""
        length = end - start
        if self.mapping.complete(start, length):
            return self.handle.read(length)
        buffer = bytearray(length)
        for low, high in self.mapping.ranges(start, end):
            self.handle.seek(low)
            part = self.handle.read(high - low)
            if len(part) != high - low:
                raise OSError("Чтение образа оборвалось в блоке, отмеченном как прочитанный.")
            buffer[low - start:high - start] = part
        self.handle.seek(end)
        return bytes(buffer)

    def fileno(self):
        return self.handle.fileno()
