"""Recover two PNG extents by verifying CRCs at the split and through IEND."""

import re
import struct
import zlib
from dataclasses import dataclass

from formats import PNG, dimensions

IDAT = re.compile(rb"\x00.{3}IDAT", re.DOTALL)
MAX_IDAT_OFFSETS = 220_000
MAX_PNG_TRIES = 400_000


@dataclass(frozen=True)
class PngAssembly:
    payload: bytes
    size: tuple[int, int]
    extents: tuple[tuple[int, int], ...]
    attempts: int


def chunk(data, cursor):
    if cursor < 8 or cursor + 12 > len(data):
        return None
    size, name = struct.unpack_from(">I4s", data, cursor)
    end = cursor + 12 + size
    if end > len(data) or size > 0x7fffffff or not name.isalpha():
        return None
    actual = zlib.crc32(memoryview(data)[cursor + 4:end - 4])
    expected, = struct.unpack_from(">I", data, end - 4)
    return (end, name, size) if actual == expected else None


def remainder(data, offset, cancelled):
    cursor = offset
    while current := chunk(data, cursor):
        if cancelled():
            return None
        cursor, name, size = current
        if name == b"IEND":
            return cursor if size == 0 else None
    return None


def break_chunk(data, header):
    cursor = header + len(PNG)
    previous = 0
    while current := chunk(data, cursor):
        cursor, name, size = current
        if name == b"IEND":
            return None
        if name == b"IDAT":
            previous = size
    if not previous or cursor + 8 > len(data) or data[cursor + 4:cursor + 8] != b"IDAT":
        return None
    size, = struct.unpack_from(">I", data, cursor)
    if not 0 < size <= 1024 * 1024:
        return None
    return cursor, size


def glue_png(image, offset, image_size, maximum, cluster, window_mib, cancelled, mapping=None):
    radius = window_mib * 1024 * 1024
    start = max(0, offset - radius)
    stop = min(image_size, offset + radius)
    image.seek(start)
    data = image.read(stop - start)
    if len(data) != stop - start:
        raise OSError("Чтение окна сборки PNG оборвалось.")
    header = offset - start
    broken = break_chunk(data, header)
    if not broken:
        return None
    cursor, size = broken
    after = cursor + 12 + size
    first_boundary = ((cursor - header + 8 + cluster - 1) // cluster) * cluster + header
    attempts = 0
    idats = []
    for found in IDAT.finditer(data):
        if cancelled():
            return None
        if len(idats) == MAX_IDAT_OFFSETS:
            return None
        point = found.start()
        if point >= header and point <= cursor:
            continue
        if not chunk(data, point):
            continue
        idats.append(point)

    # The next intact IDAT reveals the end of the broken chunk in a moved extent.
    # Its CRC lets us test both forward and backward joins without decoding every candidate.
    for boundary in range(first_boundary, min(after, len(data) + 1), cluster):
        remaining = after - boundary
        if remaining < 4:
            continue
        prefix_crc = zlib.crc32(memoryview(data)[cursor + 4:boundary])
        for next_chunk in idats:
            if cancelled():
                return None
            tail = next_chunk - remaining
            if tail < 0 or (tail - header) % cluster or (header <= tail < boundary):
                continue
            attempts += 1
            if attempts > MAX_PNG_TRIES:
                return None
            expected, = struct.unpack_from(">I", data, next_chunk - 4)
            calculated = zlib.crc32(memoryview(data)[tail:next_chunk - 4], prefix_crc)
            if calculated != expected:
                continue
            end = remainder(data, next_chunk, cancelled)
            if end is None or (tail < boundary and header < end) or boundary - header + end - tail > maximum:
                continue
            extents = ((offset, boundary - header), (start + tail, end - tail))
            if mapping and any(not mapping.complete(pos, length) for pos, length in extents):
                continue
            payload = data[header:boundary] + data[tail:end]
            size_found = dimensions(payload, "png", cancelled)
            if size_found:
                return PngAssembly(payload, size_found, extents, attempts)
    return None
