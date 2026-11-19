"""Exact Huffman/MCU validation for single-scan 8-bit baseline JPEG."""

from dataclasses import dataclass
import re

MARKER = re.compile(rb"\xff+(?![\x00\xff])[\x01-\xfe]")


class InvalidJPEG(ValueError):
    pass


class Huffman(dict):
    """Canonical codes plus a non-consuming eight-bit prefix lookup."""
    def __init__(self):
        super().__init__()
        self.prefix = [None] * 256


@dataclass(frozen=True)
class Baseline:
    start: int
    width: int
    height: int
    mcus: int
    restart: int
    blocks: tuple


def huffman_table(lengths, values):
    codes = Huffman()
    code = 0
    index = 0
    for width, count in enumerate(lengths, 1):
        if code + count >= (1 << width) and count:
            raise InvalidJPEG("invalid Huffman code")
        for value in values[index:index + count]:
            codes[(1 << width) | code] = value
            if width <= 8:
                first = code << (8 - width)
                codes.prefix[first:first + (1 << (8 - width))] = [(width, value)] * (1 << (8 - width))
            code += 1
        index += count
        code <<= 1
    return codes


def read_header(data):
    if data[:2] != b"\xff\xd8":
        return None
    tables = {}
    components = {}
    quantization = set()
    restart = width = height = 0
    cursor = 2
    try:
        while cursor + 4 <= len(data) and data[cursor] == 0xff:
            while cursor + 1 < len(data) and data[cursor + 1] == 0xff:
                cursor += 1
            marker = data[cursor + 1]
            length = int.from_bytes(data[cursor + 2:cursor + 4], "big")
            end = cursor + 2 + length
            if length < 2 or end > len(data):
                return None
            segment = data[cursor + 4:end]
            if marker == 0xc0:
                if components or len(segment) < 6 or segment[0] != 8:
                    return None
                height = int.from_bytes(segment[1:3], "big")
                width = int.from_bytes(segment[3:5], "big")
                count = segment[5]
                if count not in (1, 3, 4) or len(segment) != 6 + count * 3:
                    return None
                if not 0 < width * height <= 40_000_000:
                    return None
                for k in range(6, len(segment), 3):
                    ident, sampling, table = segment[k:k + 3]
                    h, v = sampling >> 4, sampling & 15
                    if ident in components or not (1 <= h <= 4 and 1 <= v <= 4 and table <= 3):
                        return None
                    components[ident] = (h, v, table)
            elif marker == 0xdb:
                for index in range(0, len(segment), 65):
                    entry = segment[index:index + 65]
                    if len(entry) != 65 or entry[0] > 3 or 0 in entry[1:]:
                        return None
                    quantization.add(entry[0])
            elif marker == 0xc4:
                position = 0
                while position < len(segment):
                    ident = segment[position]
                    lengths = segment[position + 1:position + 17]
                    symbols = sum(lengths)
                    start = position + 17
                    if ident not in (0, 1, 2, 3, 16, 17, 18, 19) or not 0 < symbols <= 256 or start + symbols > len(segment):
                        return None
                    tables[ident] = huffman_table(lengths, segment[start:start + symbols])
                    position = start + symbols
            elif marker == 0xdd:
                if len(segment) != 2:
                    return None
                restart = int.from_bytes(segment, "big")
            elif marker == 0xda:
                if not components or len(segment) < 4 or segment[0] != len(components):
                    return None
                if len(segment) != 4 + 2 * segment[0] or segment[-3:] != b"\x00\x3f\x00":
                    return None
                hmax = max(value[0] for value in components.values())
                vmax = max(value[1] for value in components.values())
                seen = set()
                blocks = []
                for k in range(1, len(segment) - 3, 2):
                    component, selector = segment[k:k + 2]
                    h, v, quant = components[component]
                    if component in seen or quant not in quantization:
                        return None
                    seen.add(component)
                    blocks.extend([(tables[selector >> 4], tables[16 + (selector & 15)])]
                                  * (1 if len(components) == 1 else h * v))
                if len(blocks) > 10:
                    return None
                unit_w, unit_h = (8, 8) if len(components) == 1 else (8 * hmax, 8 * vmax)
                mcus = ((width + unit_w - 1) // unit_w) * ((height + unit_h - 1) // unit_h)
                return Baseline(end, width, height, mcus, restart, tuple(blocks))
            elif not (0xe0 <= marker <= 0xef or marker == 0xfe):
                return None
            cursor = end
    except (KeyError, InvalidJPEG):
        return None
    return None


class Bits:
    def __init__(self, data):
        self.data = data
        self.position = 0
        self.left = 0
        self.value = 0

    def take(self, count):
        while self.left < count:
            value = self.data[self.position]
            self.position += 1
            self.value = (self.value << 8) | value
            self.left += 8
        self.left -= count
        value = self.value >> self.left
        self.value &= (1 << self.left) - 1
        return value

    def symbol(self, table):
        if self.left < 8 and self.position < len(self.data):
            self.value = (self.value << 8) | self.data[self.position]
            self.position += 1
            self.left += 8
        if self.left >= 8:
            match = table.prefix[self.value >> (self.left - 8)]
            if match is not None:
                width, value = match
                self.left -= width
                self.value &= (1 << self.left) - 1
                return value
        code = 1
        for _ in range(16):
            code = (code << 1) | self.take(1)
            value = table.get(code)
            if value is not None:
                return value
        raise InvalidJPEG("unassigned Huffman code")

    def padding(self):
        return (self.left < 8 and self.position == len(self.data)
                and self.value == (1 << self.left) - 1)


def scan_body(payload, header):
    end = len(payload) - 2
    while end > header.start and payload[end - 1] == 255:
        end -= 1
    return payload[header.start:end]


def valid_entropy(encoded, header, cancelled, limit=None):
    """Check exact MCU count, Huffman codes, coefficient bounds and restart order."""
    if limit is not None and header.mcus * len(header.blocks) > limit:
        return False
    segments = MARKER.finditer(encoded)
    first = 0
    remaining = header.mcus
    marker_number = 0
    try:
        while remaining:
            count = min(remaining, header.restart) if header.restart else remaining
            marker = next(segments, None)
            code = marker.group()[-1] if marker else 0
            if remaining > count:
                if code != 0xd0 + marker_number % 8:
                    return False
            elif marker is not None:
                return False
            stop = marker.start() if marker else len(encoded)
            reader = Bits(encoded[first:stop].replace(b"\xff\x00", b"\xff"))
            for mcu in range(count):
                if mcu % 64 == 0 and cancelled():
                    return False
                for dc, ac in header.blocks:
                    width = reader.symbol(dc)
                    if width > 11:
                        return False
                    reader.take(width)
                    coefficient = 1
                    while coefficient < 64:
                        value = reader.symbol(ac)
                        if not value:
                            break
                        run, width = value >> 4, value & 15
                        coefficient += run
                        if coefficient >= 64 or width > 10 or (width == 0 and run != 15):
                            return False
                        reader.take(width)
                        coefficient += 1
            if not reader.padding():
                return False
            remaining -= count
            if marker:
                first = marker.end()
                marker_number += 1
        return True
    except (IndexError, InvalidJPEG):
        return False
