"""Bounded, validated reconstruction of a selected ZIP/Office file."""
from dataclasses import dataclass, field
import io
import itertools
import time
import zipfile
import zlib

from fragments import Assembly, FILL_GAP


@dataclass
class ZipSearch:
    matches: list = field(default_factory=list)
    attempts: int = 0
    capped: bool = False


def valid_zip(payload, cancelled, budget=None):
    if not payload.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return False
    eocd = payload.rfind(b"PK\x05\x06", max(0, len(payload) - 65557))
    if eocd < 0 or eocd + 22 > len(payload):
        return False
    if eocd + 22 + int.from_bytes(payload[eocd + 20:eocd + 22], "little") != len(payload):
        return False
    # Reject prefix compensation accepted by zipfile; offsets must describe
    # this exact byte stream, not a concatenated/self-extracting archive.
    central_size = int.from_bytes(payload[eocd + 12:eocd + 16], "little")
    central_start = int.from_bytes(payload[eocd + 16:eocd + 20], "little")
    if central_start + central_size != eocd:
        return False  # ZIP64 and multi-volume archives are not reconstructed.
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(e.file_size for e in entries) > 512 * 1024 * 1024:
                return False
            for entry in entries:
                if entry.flag_bits & 1:
                    return False
                with archive.open(entry) as member:
                    while True:
                        if cancelled():
                            return False
                        chunk = member.read(65536)
                        if not chunk:
                            break
                        if budget is not None:
                            budget[0] -= len(chunk)
                            if budget[0] < 0:
                                return False
        return True
    except (zipfile.BadZipFile, RuntimeError, ValueError, OSError, EOFError, NotImplementedError, zlib.error):
        return False


def glue_zip(image, offset, image_size, expected_size, cluster, window_mib, cancelled, mapping=None):
    found = ZipSearch()
    window = min(image_size - offset, window_mib * 1024 * 1024)
    if expected_size > window or expected_size > 64 * 1024 * 1024:
        found.capped = True
        return found
    image.seek(offset)
    data = image.read(window)
    gaps = []
    for fill in FILL_GAP.finditer(data):
        low = ((fill.start() + cluster - 1) // cluster) * cluster
        high = fill.end() // cluster * cluster
        if 0 < low < expected_size and high > low:
            gaps.append((low, high))
            if len(gaps) == 8:
                found.capped = True
                break
    budget = [512 * 1024 * 1024]
    deadline = time.monotonic() + 20
    def stopped():
        return cancelled() or time.monotonic() >= deadline or budget[0] < 0
    for count in range(1, min(4, len(gaps)) + 1):
        for removed in itertools.combinations(gaps, count):
            if stopped() or found.attempts >= 128:
                found.capped = not cancelled()
                return found
            cursor, remaining, parts = 0, expected_size, []
            for low, high in removed:
                length = low - cursor
                if length <= 0 or length >= remaining:
                    break
                parts.append((offset + cursor, length))
                remaining -= length
                cursor = high
            else:
                if cursor + remaining > len(data):
                    continue
                parts.append((offset + cursor, remaining))
                if mapping and any(not mapping.complete(start, length) for start, length in parts):
                    continue
                payload = b"".join(data[start - offset:start - offset + length] for start, length in parts)
                found.attempts += 1
                if valid_zip(payload, stopped, budget):
                    found.matches.append(Assembly(payload, (0, 0), tuple(parts)))
                    if len(found.matches) >= 8:
                        found.capped = True
                        return found
    return found
