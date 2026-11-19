"""Bounded baseline JPEG assembly with forward gaps."""

import hashlib
import itertools
import time
import re
from dataclasses import dataclass

from formats import dimensions, jpeg_parts
from jpeg_validation import MARKER, read_header, valid_entropy, scan_body

EOI = re.compile(rb"\xff\xd9")
FILL_GAP = re.compile(rb"(.)\1{511,}", re.DOTALL)
OBJECT_HEADER = re.compile(rb"\xff\xd8\xff|BM")
MAX_MATCHES = 8
CLUSTER_SIZES = {512, 1024, 2048, 4096, 8192, 16384, 32768, 65536}
MAX_WINDOW_MIB = 32
MAX_CANDIDATES = 1500
MAX_WORK_BLOCKS = 900_000
MAX_ENDS = 520
MAX_SPLITS = 512
MAX_HEADER = 256 * 1024


@dataclass(frozen=True)
class Assembly:
    payload: bytes
    size: tuple[int, int]
    extents: tuple[tuple[int, int], ...]


@dataclass
class Search:
    matches: list[Assembly]
    attempts: int = 0
    capped: bool = False
    supported: bool = False
    match_limit: bool = False
    partial: tuple | None = None
    read_seconds: float = 0.0
    validation_seconds: float = 0.0
    search_seconds: float = 0.0


def glue(image, offset, image_size, maximum, cluster, window_mib, cancelled, mapping=None):
    started = time.perf_counter()
    findings = _glue(image, offset, image_size, maximum, cluster, window_mib, cancelled, mapping)
    findings.search_seconds = max(0.0, time.perf_counter() - started - findings.read_seconds - findings.validation_seconds)
    return findings


def _glue(image, offset, image_size, maximum, cluster, window_mib, cancelled, mapping=None):
    findings = Search([])
    size = min(image_size - offset, window_mib * 1024 * 1024)
    started = time.perf_counter()
    image.seek(offset)
    data = image.read(size)
    findings.read_seconds = time.perf_counter() - started
    if len(data) != size:
        raise OSError("Чтение окна сборки JPEG оборвалось.")
    header = read_header(data[:MAX_HEADER])
    if header is None:
        return findings
    findings.supported = True
    work_per_attempt = header.mcus * len(header.blocks)
    work_budget = max(MAX_WORK_BLOCKS, 3 * work_per_attempt)
    if work_per_attempt > 1_800_000:
        findings.capped = True
        return findings
    first_mark = next((m for m in MARKER.finditer(data, header.start)
                       if not (header.restart and 0xd0 <= m.group()[-1] <= 0xd7)), None)
    latest_split = first_mark.start() + 1 if first_mark else len(data)
    latest_split = min(latest_split, len(data) - cluster, maximum - 2)
    first_split = ((header.start + cluster - 1) // cluster) * cluster
    if latest_split < first_split:
        return findings
    splits = range(latest_split // cluster * cluster, first_split - 1, -cluster)
    if len(splits) > MAX_SPLITS:
        findings.capped = True
        splits = splits[:MAX_SPLITS]

    ends = []
    for mark in EOI.finditer(data, first_split + cluster):
        if cancelled():
            return findings
        if len(ends) == MAX_ENDS:
            findings.capped = True
            break
        ends.append(mark.end())
    seen = set()
    tried = set()

    def inspect(parts):
        if parts in tried:
            return True
        if findings.attempts >= MAX_CANDIDATES or (findings.attempts + 1) * work_per_attempt > work_budget:
            findings.capped = True
            return False
        tried.add(parts)
        findings.attempts += 1
        payload = data[:parts[0][1]] + b"".join(data[start:stop] for start, stop in parts[1:])
        started = time.perf_counter()
        valid = valid_entropy(scan_body(payload, header), header, cancelled)
        findings.validation_seconds += time.perf_counter() - started
        if not valid:
            return True
        digest = hashlib.sha256(payload).digest()
        if digest in seen:
            return True
        started = time.perf_counter()
        size = dimensions(payload, "jpg", cancelled)
        findings.validation_seconds += time.perf_counter() - started
        if size is not None:
            seen.add(digest)
            findings.matches.append(Assembly(payload, size, tuple(
                (offset + start, stop - start) for start, stop in parts
            )))
        if len(findings.matches) >= MAX_MATCHES:
            findings.capped = True
            findings.match_limit = True
            return False
        return True

    def try_boundaries(split, second):
        for end in ends:
            if cancelled():
                return False
            if end <= second + 2 or split + end - second > maximum:
                continue
            if not mapping or (mapping.complete(offset, split) and
                               mapping.complete(offset + second, end - second)):
                if not inspect(((0, split), (second, end))):
                    return False
        return True

    if mapping:
        # The actual physical gaps in the map are strong candidate boundaries.
        trusted = []
        for low, high in mapping.ranges(offset, offset + len(data)):
            trusted.append((low - offset, high - offset))
            if len(trusted) >= 4:
                break
        if trusted and trusted[0][0] == 0 and trusted[0][1] >= header.start:
            for index in range(1, min(3, len(trusted))):
                prefix = trusted[:index]
                tail_start, tail_end = trusted[index]
                for end in ends:
                    if cancelled():
                        return findings
                    if not tail_start + 2 < end <= tail_end:
                        continue
                    parts = (*prefix, (tail_start, end))
                    if any((start % cluster or stop % cluster) for start, stop in prefix):
                        continue
                    if tail_start % cluster or sum(stop - start for start, stop in parts) > maximum:
                        continue
                    if not inspect(parts):
                        return findings

    for object_start in OBJECT_HEADER.finditer(data, first_split, latest_split + 4):
        if cancelled():
            return findings
        split = object_start.start()
        if split % cluster or split > latest_split:
            continue
        if data[split:split + 2] == b"BM":
            # A BMP start anchors the split even when that BMP is itself fragmented.
            if split + 18 > len(data) or int.from_bytes(data[split + 14:split + 18], "little") not in (12, 40, 52, 56, 64, 108, 124):
                continue
            declared = int.from_bytes(data[split + 2:split + 6], "little")
            if declared < 26:
                continue
            second = ((split + declared + cluster - 1) // cluster) * cluster
            if not try_boundaries(split, second):
                return findings
            for end in ends:
                earliest = split + cluster
                if not header.restart:
                    for marker in MARKER.finditer(data, earliest, end - 2):
                        earliest = marker.end()
                earliest = ((earliest + cluster - 1) // cluster) * cluster
                if end - earliest > maximum - split:
                    continue
                for second in range(earliest, min(earliest + 8 * cluster, end - 2), cluster):
                    if cancelled():
                        return findings
                    if mapping and not (mapping.complete(offset, split) and
                                        mapping.complete(offset + second, end - second)):
                        continue
                    if not inspect(((0, split), (second, end))):
                        return findings
        else:
            nested_end, _ = jpeg_parts(data[split:split + maximum], cancelled)
            if nested_end is not None:
                second = ((split + nested_end + cluster - 1) // cluster) * cluster
                if not try_boundaries(split, second):
                    return findings

    # A tail can be stored before the middle. A long fill run anchors the
    # occupied range, while the JPEG end marker anchors the displaced tail.
    if not header.restart:
        for fill in FILL_GAP.finditer(data, first_split):
            if cancelled():
                return findings
            occupied = ((fill.start() + cluster - 1) // cluster) * cluster
            if occupied > min(maximum + cluster, 3 * 1024 * 1024):
                break
            for end in ends:
                middle_start = ((end + cluster - 1) // cluster) * cluster
                if end >= occupied or middle_start + cluster > occupied:
                    continue
                # Skip any unrelated JPEG or marker found between the tail and fill.
                if MARKER.search(data, middle_start, occupied):
                    continue
                if first_mark is None or first_mark.end() != end:
                    continue
                upper = min(latest_split // cluster * cluster, end - 2)
                for split in sorted(range(first_split, upper + 1, cluster),
                                    key=lambda position: abs(position * 3 - occupied))[:16]:
                    if cancelled():
                        return findings
                    parts = ((0, split), (middle_start, occupied), (split, end))
                    if mapping and any(not mapping.complete(offset + start, stop - start) for start, stop in parts):
                        continue
                    if sum(stop - start for start, stop in parts) <= maximum and not inspect(parts):
                        return findings

    for erased in FILL_GAP.finditer(data, first_split):
        if cancelled():
            return findings
        split = ((erased.start() + cluster - 1) // cluster) * cluster
        second = erased.end() // cluster * cluster
        if (findings.partial is None and not ends and erased.end() - erased.start() >= 4 * cluster
                and split <= latest_split and erased.start() == split and split - header.start >= 1024
                and (not mapping or mapping.complete(offset, split))):
            payload = data[:split] + b"\xff\xd9"
            dimensions_found = dimensions(payload, "jpg", cancelled)
            if dimensions_found:
                findings.partial = payload, "jpg", "partial_jpeg", dimensions_found, split, 0
        if split <= latest_split and second >= split + cluster and not try_boundaries(split, second):
            return findings

    # Multiple erased cluster runs can separate three or more surviving pieces.
    # Treat them as hypotheses, never as proof: every assembled byte must pass
    # the same exact entropy validator and retain its physical source extent.
    gaps = []
    for fill in FILL_GAP.finditer(data, first_split):
        if cancelled():
            return findings
        low = ((fill.start() + cluster - 1) // cluster) * cluster
        high = fill.end() // cluster * cluster
        if low >= first_split and high > low and any(end > high + 2 for end in ends):
            gaps.append((low, high))
            if len(gaps) == 8:
                findings.capped = True
                break
    for count in range(2, min(4, len(gaps)) + 1):
        for removed in itertools.combinations(gaps, count):
            for end in ends:
                if cancelled():
                    return findings
                if end <= removed[-1][1] + 2:
                    continue
                parts = []
                previous = 0
                for low, high in removed:
                    if low <= previous:
                        break
                    parts.append((previous, low))
                    previous = high
                else:
                    parts.append((previous, end))
                    if sum(stop - start for start, stop in parts) > maximum:
                        continue
                    if mapping and any(not mapping.complete(offset + start, stop - start)
                                       for start, stop in parts):
                        continue
                    if not inspect(tuple(parts)):
                        return findings

    for end in ends:
        if cancelled():
            return findings
        # Candidate suffix starts at a cluster boundary. Restrict it to the
        # current marker-free entropy run when no restart markers are allowed.
        earliest_second = first_split + cluster
        if not header.restart:
            last_limit = max(earliest_second, end - maximum + first_split)
            for marker in MARKER.finditer(data, last_limit, end - 2):
                earliest_second = marker.end()
        first_tail = ((max(first_split + cluster, earliest_second) + cluster - 1) // cluster) * cluster
        for split in splits:
            if cancelled():
                return findings
            if split + cluster > end - 2:
                continue
            if mapping and not mapping.complete(offset, split):
                continue
            lower = max(first_tail, split + cluster, ((end - maximum + split + cluster - 1) // cluster) * cluster)
            for second in range(lower, end - 1, cluster):
                if cancelled():
                    return findings
                if mapping and not mapping.complete(offset + second, end - second):
                    continue
                if not inspect(((0, split), (second, end))):
                    return findings
    return findings
