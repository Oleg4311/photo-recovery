"""Bounded photo carving, container validation, and EXIF preview extraction."""

import io
import re
import struct
import warnings

from PIL import Image

PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
SIGNATURES = re.compile(rb"\xff\xd8\xff|\x89PNG\r\n\x1a\n|RIFF")
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp"}
ENTROPY_MARKER = re.compile(rb"\xff+(?![\x00\xff])([\x01-\xfe])")
MAX_PIXELS = 40_000_000
ZERO_RUN = re.compile(rb"\x00{512,}")
MIN_ENTROPY_PREFIX = 1024


def kind(prefix: bytes) -> str | None:
    if prefix.startswith(JPEG):
        return "jpg"
    if prefix.startswith(PNG):
        return "png"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "webp"
    return None


def jpeg_parts(data: bytes, cancelled) -> tuple[int | None, list[tuple[int, bytes]]]:
    thumbnails = []
    cursor = 2
    saw_scan = False
    in_entropy = False
    while cursor + 1 < len(data) and not cancelled():
        if in_entropy:
            marker = ENTROPY_MARKER.search(data, cursor)
            if marker is None:
                break
            cursor = marker.end(1) - 1
            code = data[cursor]
            cursor += 1
            if 0xd0 <= code <= 0xd7 or code == 0x01:
                continue
            in_entropy = False
        else:
            if data[cursor] != 0xff:
                break
            while cursor < len(data) and data[cursor] == 0xff:
                cursor += 1
            if cursor == len(data):
                break
            code = data[cursor]
            cursor += 1
        if code == 0xd9:
            return (cursor if saw_scan else None), thumbnails
        if code in (0, 0xd8) or 0xd0 <= code <= 0xd7:
            break
        if code == 0x01:
            continue
        length = int.from_bytes(data[cursor:cursor + 2], "big")
        end = cursor + length
        if length < 2 or end > len(data):
            # An APP1 segment may outlive the rest of the image in a short file.
            if code == 0xe1:
                preview = exif_thumbnail(data[cursor + 2:], cursor + 2)
                if preview:
                    thumbnails.append(preview)
            break
        if code == 0xe1:
            preview = exif_thumbnail(data[cursor + 2:end], cursor + 2)
            if preview:
                thumbnails.append(preview)
        if code == 0xda:
            saw_scan = True
            in_entropy = True
        cursor = end
    return None, thumbnails


def exif_thumbnail(payload: bytes, base: int) -> tuple[int, bytes] | None:
    if not payload.startswith(b"Exif\0\0"):
        return None
    tiff = payload[6:]
    endian = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if not endian:
        return None
    try:
        magic, first = struct.unpack_from(endian + "HI", tiff, 2)
        if magic != 42 or not 8 <= first < len(tiff):
            return None
        count, = struct.unpack_from(endian + "H", tiff, first)
        second, = struct.unpack_from(endian + "I", tiff, first + 2 + 12 * count)
        if not 8 <= second < len(tiff):
            return None
        count, = struct.unpack_from(endian + "H", tiff, second)
        if second + 2 + count * 12 + 4 > len(tiff):
            return None
        values = {}
        for cursor in range(second + 2, second + 2 + count * 12, 12):
            tag, field_type, items, value = struct.unpack_from(endian + "HHII", tiff, cursor)
            if tag in (0x0201, 0x0202) and field_type == 4 and items == 1:
                values[tag] = value
        offset, size = values.get(0x0201, 0), values.get(0x0202, 0)
        if offset >= 8 and 4 <= size <= len(tiff) - offset:
            photo = tiff[offset:offset + size]
            if photo.startswith(JPEG):
                return base + 6 + offset, photo
    except struct.error:
        return None
    return None


def png_end(data: bytes, cancelled) -> int | None:
    cursor = len(PNG)
    while cursor + 12 <= len(data) and not cancelled():
        size = int.from_bytes(data[cursor:cursor + 4], "big")
        marker = data[cursor + 4:cursor + 8]
        if cursor == len(PNG) and (marker != b"IHDR" or size != 13):
            break
        end = cursor + 12 + size
        if end > len(data) or size > 0x7fffffff:
            break
        if marker == b"IEND":
            if size == 0 and data[cursor + 8:end] == b"\xaeB\x60\x82":
                return end
            break
        cursor = end
    return None


def webp_end(data: bytes, cancelled) -> int | None:
    end = 8 + int.from_bytes(data[4:8], "little")
    if not 20 <= end <= len(data) or end % 2:
        return None
    cursor = 12
    while cursor + 8 <= end and not cancelled():
        size = int.from_bytes(data[cursor + 4:cursor + 8], "little")
        cursor += 8 + size + (size % 2)
    return end if cursor == end else None


def dimensions(data: bytes, extension: str, cancelled) -> tuple[int, int] | None:
    if cancelled():
        return None
    image_format = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}[extension]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=[image_format]) as image:
                width, height = image.size
                frames = getattr(image, "n_frames", 1)
                if width * height > MAX_PIXELS or width * height * frames > MAX_PIXELS * 4:
                    return None
                image.verify()
            with Image.open(io.BytesIO(data), formats=[image_format]) as image:
                for frame in range(frames):
                    if cancelled():
                        return None
                    image.seek(frame)
                    image.load()
        return width, height
    except (OSError, ValueError, SyntaxError, EOFError, OverflowError, struct.error,
            Image.DecompressionBombWarning, Image.DecompressionBombError):
        return None


def zeroed_jpeg(data: bytes, cancelled):
    """Preserve a JPEG byte prefix before a long erased block, when it decodes."""
    if cancelled():
        return None
    cursor = 2
    while cursor + 4 <= len(data) and data[cursor] == 0xff:
        code = data[cursor + 1]
        if code in (0x00, 0xd8, 0xd9) or 0xd0 <= code <= 0xd7:
            return None
        if code in (0x01, 0xff):
            cursor += 1 if code == 0xff else 2
            continue
        length = int.from_bytes(data[cursor + 2:cursor + 4], "big")
        if length < 2:
            return None
        cursor += 2 + length
        # The initial scan is the only prefix we can reason about conservatively.
        if code == 0xda:
            zeroes = ZERO_RUN.search(data, cursor)
            if zeroes is None or zeroes.start() - cursor < MIN_ENTROPY_PREFIX:
                return None
            natural = ENTROPY_MARKER.search(data, cursor, zeroes.start())
            while natural is not None and 0xd0 <= natural.group(1)[0] <= 0xd7:
                natural = ENTROPY_MARKER.search(data, natural.end(), zeroes.start())
            if natural is not None:
                return None
            return zeroes.start()
    return None


def extract(data: bytes, cancelled, repair_end: bool = False, salvage: bool = True):
    """Yield decoded bytes, category, original byte length, and relative offset."""
    extension = kind(data[:12])
    previews = []
    if extension == "jpg":
        end, previews = jpeg_parts(data, cancelled)
    elif extension == "png":
        end = png_end(data, cancelled)
    elif extension == "webp":
        end = webp_end(data, cancelled)
    else:
        return

    zero_at = zeroed_jpeg(data[:end], cancelled) if extension == "jpg" and salvage else None
    if zero_at is not None:
        payload = data[:zero_at] + b"\xff\xd9"
        size = dimensions(payload, extension, cancelled)
        if size:
            yield payload, extension, "partial_jpeg", size, zero_at, 0
    elif end is not None:
        payload = data[:end]
        size = dimensions(payload, extension, cancelled)
        if size:
            yield payload, extension, "decoded", size, end, 0
    elif extension == "jpg" and repair_end and len(data) >= 16:
        payload = data + b"\xff\xd9"
        corrected_end, _ = jpeg_parts(payload, cancelled)
        if corrected_end == len(payload):
            size = dimensions(payload, extension, cancelled)
            if size:
                yield payload, extension, "repaired_eoi", size, len(data), 0

    for relative, preview in previews:
        end, _ = jpeg_parts(preview, cancelled)
        if end is not None:
            payload = preview[:end]
            size = dimensions(payload, "jpg", cancelled)
            if size:
                yield payload, "jpg", "exif_thumbnail", size, end, relative
