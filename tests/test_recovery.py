import hashlib
import io
import json
import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from formats import extract
from recovery import BLOCK_SIZE, scan


def put(data, offset, value, length):
    data[offset:offset + length] = value.to_bytes(length, "little")


def photo(width, height, fmt="JPEG"):
    stream = io.BytesIO()
    rng = random.Random(405 + width)
    image = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    image.save(stream, fmt, quality=84)
    return stream.getvalue()


def with_thumbnail(main, thumbnail):
    ifd0 = struct.pack("<HI", 0, 14)
    ifd1 = struct.pack("<H", 2) + struct.pack("<HHII", 0x0201, 4, 1, 44)
    ifd1 += struct.pack("<HHII", 0x0202, 4, 1, len(thumbnail)) + bytes(4)
    payload = b"Exif\0\0II" + struct.pack("<HI", 42, 8) + ifd0 + ifd1 + thumbnail
    return main[:2] + b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload + main[2:]


def resident(kind, value):
    size = (24 + len(value) + 7) // 8 * 8
    attr = bytearray(size)
    put(attr, 0, kind, 4)
    put(attr, 4, size, 4)
    put(attr, 16, len(value), 4)
    put(attr, 20, 24, 2)
    attr[24:24 + len(value)] = value
    return attr


def filename(name):
    value = bytearray(66)
    value[64], value[65] = len(name), 1
    return resident(0x30, value + name.encode("utf-16-le"))


def nonresident(runs, size):
    pairs = bytearray()
    previous = 0
    for cluster, count in runs:
        pairs += bytes((0x21, count)) + (cluster - previous).to_bytes(2, "little", signed=True)
        previous = cluster
    pairs += b"\0"
    attr = bytearray((64 + len(pairs) + 7) // 8 * 8)
    put(attr, 0, 0x80, 4)
    put(attr, 4, len(attr), 4)
    attr[8] = 1
    clusters = sum(count for _, count in runs)
    put(attr, 24, clusters - 1, 8)
    put(attr, 32, 64, 2)
    put(attr, 40, clusters * 512, 8)
    put(attr, 48, size, 8)
    put(attr, 56, size, 8)
    attr[64:64 + len(pairs)] = pairs
    return attr


def mft_record(active, *attrs):
    record = bytearray(1024)
    record[:4] = b"FILE"
    put(record, 4, 48, 2)
    put(record, 6, 3, 2)
    put(record, 20, 56, 2)
    put(record, 22, int(active), 2)
    cursor = 56
    for attr in attrs:
        record[cursor:cursor + len(attr)] = attr
        cursor += len(attr)
    put(record, cursor, 0xffffffff, 4)
    put(record, 24, cursor + 8, 4)
    record[48:50] = b"\x18\x57"
    for i in range(1, 3):
        end = 512 * i
        record[48 + 2 * i:50 + 2 * i] = record[end - 2:end]
        record[end - 2:end] = record[48:50]
    return record


def ntfs_image(main, damaged):
    image = bytearray(512 * 400)
    image[3:11] = b"NTFS    "
    image[510:512] = b"\x55\xaa"
    put(image, 11, 512, 2)
    put(image, 13, 1, 1)
    put(image, 40, 400, 8)
    put(image, 48, 4, 8)
    put(image, 56, 48, 8)
    put(image, 64, 246, 1)
    base = mft_record(True, nonresident([(4, 20), (26, 40)], 30 * 1024))
    image[4 * 512:6 * 512] = base
    image[48 * 512:50 * 512] = base
    image[16 * 512:18 * 512] = mft_record(True, nonresident([(80, 1)], 50))

    def install(index, name, payload, start, second):
        length = (len(payload) + 511) // 512
        record = mft_record(False, filename(name), nonresident([(start, 1), (second, length - 1)], len(payload)))
        location = (26 + (index - 10) * 2) * 512
        image[location:location + 1024] = record
        image[start * 512:(start + 1) * 512] = payload[:512]
        image[second * 512:second * 512 + len(payload) - 512] = payload[512:]

    install(25, "ночь.jpg", main, 100, 170)
    install(26, "сломанный.jpg", damaged, 133, 250)
    return image


def flash_image(payload, exfat):
    image = bytearray(512 * 108)
    image[510:512] = b"\x55\xaa"
    if exfat:
        image[3:11] = b"EXFAT   "
        for offset, value in ((72, 108), (80, 1), (84, 1), (88, 2), (92, 106), (96, 2)):
            put(image, offset, value, 8 if offset == 72 else 4)
        image[108:111] = bytes((9, 0, 1))
    else:
        image[82:90] = b"FAT32   "
        for offset, value, size in ((11, 512, 2), (13, 1, 1), (14, 1, 2), (16, 1, 1),
                                    (32, 108, 4), (36, 1, 4), (44, 2, 4)):
            put(image, offset, value, size)
    put(image, 512 + 2 * 4, 0xffffffff, 4)
    start = 6
    root = 1024
    if exfat:
        bitmap = bytearray(32)
        bitmap[0] = 0x81
        put(bitmap, 20, 3, 4)
        put(bitmap, 24, 14, 8)
        put(image, 512 + 3 * 4, 0xffffffff, 4)
        image[root:root + 32] = bitmap
        image[1536] = 3
        root += 32
        first, stream, name = bytearray(32), bytearray(32), bytearray(32)
        first[:2] = bytes((0x85, 2))
        label = "Карта.png"
        stream[:4] = bytes((0xc0, 3, 0, len(label)))
        put(stream, 8, len(payload), 8)
        put(stream, 20, start, 4)
        put(stream, 24, len(payload), 8)
        name[0] = 0xc1
        utf = label.encode("utf-16-le")
        name[2:2 + len(utf)] = utf
        checksum = 0
        for block_number, block in enumerate((first, stream, name)):
            for index, byte in enumerate(block):
                if block_number == 0 and index in (2, 3):
                    continue
                checksum = (((checksum >> 1) | (checksum << 15)) + byte) & 0xffff
        put(first, 2, checksum, 2)
        for entry in (first, stream, name):
            entry[0] &= 0x7f
        image[root:root + 96] = first + stream + name
    else:
        image[root:root + 11] = b"\xe5HOTOS  PNG"
        put(image, root + 26, start, 2)
        put(image, root + 28, len(payload), 4)
    physical = 1024 + (start - 2) * 512
    image[physical:physical + len(payload)] = payload
    return image


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "source.img"

    def run_image(self, data, **kwargs):
        self.image.write_bytes(data)
        digest = hashlib.sha256(data).digest()
        result = scan(self.image, self.root, **kwargs)
        self.assertEqual(hashlib.sha256(self.image.read_bytes()).digest(), digest)
        records = [json.loads(line) for line in (result.folder / "report.jsonl").read_text().splitlines()]
        return result, [entry for entry in records if entry["event"] == "saved"]

    def test_ntfs_fragments_and_repaired_end_with_exact_bytes(self):
        jpeg = photo(120, 90)
        thumbnail = photo(24, 20)
        truncated = with_thumbnail(photo(140, 100), thumbnail)[:-2]
        result, records = self.run_image(ntfs_image(jpeg, truncated))
        by_name = {entry["file"].split("/")[0]: entry for entry in records}
        self.assertEqual(result.volumes, 1)
        self.assertEqual(by_name["decoded"]["original_name"], "ночь.jpg")
        self.assertEqual(by_name["decoded"]["allocation"], "free")
        self.assertEqual(len(by_name["decoded"]["extents"]), 2)
        self.assertEqual((result.folder / by_name["decoded"]["file"]).read_bytes(), jpeg)
        self.assertEqual((result.folder / by_name["repaired_eoi"]["file"]).read_bytes(), truncated + b"\xff\xd9")
        self.assertEqual((result.folder / by_name["exif_thumbnail"]["file"]).read_bytes(), thumbnail)

    def test_destroyed_photo_with_surviving_exif(self):
        thumbnail = photo(36, 30)
        data = with_thumbnail(photo(100, 100), thumbnail)
        app1_end = 4 + int.from_bytes(data[4:6], "big")
        corrupted = data[:app1_end] + bytes(9000)
        result, records = self.run_image(b"A" * 777 + corrupted, metadata=False)
        self.assertEqual(result.decoded, 0)
        self.assertEqual(result.thumbnails, 1)
        self.assertEqual((result.folder / records[0]["file"]).read_bytes(), thumbnail)

    def test_partially_overwritten_jpeg(self):
        jpeg = photo(180, 130)
        cut = len(jpeg) * 2 // 3
        result, records = self.run_image(bytes(1536) + jpeg[:cut] + bytes(len(jpeg) - cut), metadata=False)
        self.assertEqual((result.decoded, result.partial), (0, 1))
        recovered = (result.folder / records[0]["file"]).read_bytes()
        self.assertEqual(recovered, jpeg[:cut] + b"\xff\xd9")
        with Image.open(io.BytesIO(recovered)) as image:
            image.load()
            self.assertEqual(image.size, (180, 130))

    def test_signatures_crossing_chunk_boundary_and_dedup(self):
        png = photo(45, 40, "PNG")
        webp = photo(55, 44, "WEBP")
        result, records = self.run_image(b"Z" * (BLOCK_SIZE - 2) + png + bytes(23) + webp + png, metadata=False)
        self.assertEqual((result.decoded, result.duplicates), (2, 1))
        self.assertEqual(records[0]["image_offset"], BLOCK_SIZE - 2)
        self.assertEqual({(result.folder / record["file"]).read_bytes() for record in records}, {png, webp})

    def test_deleted_fat32_in_mbr_and_exfat_volume(self):
        png = photo(24, 22, "PNG")
        for exfat in (True, False):
            with self.subTest(exfat=exfat):
                data = flash_image(png, exfat)
                if not exfat:
                    mbr = bytearray(512 * 16)
                    mbr[510:512] = b"\x55\xaa"
                    mbr[450] = 0x0c
                    put(mbr, 454, 16, 4)
                    data = mbr + data
                result, records = self.run_image(data, raw=False)
                self.assertEqual((result.volumes, result.decoded), (1, 1))
                record = records[0]
                self.assertEqual(record["method"], "exfat" if exfat else "fat32")
                self.assertEqual(record["allocation"], "free")
                self.assertEqual(record["mapping"], "contiguous_declared" if exfat else "contiguous_guess")
                self.assertEqual((result.folder / record["file"]).read_bytes(), png)

    def test_existing_copy_after_source_overwrite(self):
        jpg = photo(36, 34)
        copies = self.root / "backup"
        copies.mkdir()
        (copies / "message.jpg").write_bytes(jpg)
        result, records = self.run_image(bytes(80_000), copies=copies)
        self.assertEqual(result.copies_checked, 1)
        self.assertEqual(records[0]["method"], "existing_copy")
        self.assertEqual((result.folder / records[0]["file"]).read_bytes(), jpg)
        self.assertEqual(scan(None, self.root, copies=copies).decoded, 1)

    def test_carve_windows_thumbnail_cache_past_size_limit(self):
        jpg = photo(36, 34)
        copies = self.root / "backup"
        copies.mkdir()
        database = copies / "thumbcache_256.db"
        database.write_bytes(b"CMMM" + bytes(2 * 1024 * 1024) + jpg)
        result, records = self.run_image(bytes(80_000), max_mib=1, copies=copies)
        self.assertEqual((result.cache_previews, result.decoded), (1, 0))
        self.assertEqual(records[0]["method"], "windows_cache")
        self.assertEqual((result.folder / records[0]["file"]).read_bytes(), jpg)

    def test_fragment_carving_improves_on_signature_scan(self):
        payload = photo(120, 90)
        split = 2048
        start = 8192
        gap = random.Random(28).randbytes(3072)
        second = start + split + len(gap)
        packed = bytes(start) + payload[:split] + gap + payload[split:] + bytes(512)
        off, records = self.run_image(packed, metadata=False)
        self.assertNotIn(payload, [(off.folder / entry["file"]).read_bytes() for entry in records])
        on, records = self.run_image(packed, metadata=False, deep=True, search_mib=1)
        self.assertEqual(on.assembled, 1)
        result = next(item for item in records if item["method"] == "jpeg_fragments")
        self.assertEqual(result["extents"], [[start, split], [second, len(payload) - split]])
        self.assertEqual((on.folder / result["file"]).read_bytes(), payload)

    def test_zero_gap_is_checked_when_other_stitches_decode(self):
        payload = photo(155, 108)
        split = 5120
        prefix = bytes(4096)
        packed = prefix + payload[:split] + bytes(3584) + payload[split:] + bytes(512)
        result, records = self.run_image(packed, metadata=False, deep=True, search_mib=1)
        candidates = [entry for entry in records if entry["method"] == "jpeg_fragments"]
        self.assertIn(payload, [(result.folder / entry["file"]).read_bytes() for entry in candidates])
        self.assertTrue(all(entry["image_offset"] == len(prefix) for entry in candidates))

    def test_three_fragments_without_rescue_map(self):
        payload = photo(192, 144)
        start = 1024
        packed = (bytes(start) + payload[:2048] + bytes(1024)
                  + payload[2048:6144] + b"X" * 1536 + payload[6144:])
        result, records = self.run_image(packed, metadata=False, deep=True, search_mib=1)
        recovered = next(row for row in records if row["sha256"] == hashlib.sha256(payload).hexdigest())
        self.assertEqual(recovered["extents"], [[start, 2048], [start + 3072, 4096],
                                              [start + 8704, len(payload) - 6144]])
        self.assertEqual((result.folder / recovered["file"]).read_bytes(), payload)
        self.assertFalse(recovered["authenticity_verified"])

    def test_fragment_carving_skips_false_eoi_and_supports_restart(self):
        stream = io.BytesIO()
        with Image.open(io.BytesIO(photo(128, 96))) as image:
            image.save(stream, "JPEG", quality=85, restart_marker_blocks=3)
        payload = stream.getvalue()
        split = 4096
        gap = b"\xff\xd9" + random.Random(29).randbytes(1022)
        packed = bytes(1024) + payload[:split] + gap + payload[split:] + bytes(512)
        result, records = self.run_image(packed, metadata=False, deep=True, search_mib=1)
        assembled = [item for item in records if item["category"] == "assembled_jpeg"]
        self.assertEqual(len(assembled), 1)
        self.assertEqual((result.folder / assembled[0]["file"]).read_bytes(), payload)

    def test_rescue_map_blocks_stale_data_and_joins_three_fragments(self):
        payload = photo(160, 120)
        start = 4096
        first_stop, second_stop = 2048, 6144
        stale = photo(20, 20)
        first_gap = stale + bytes(2048 - len(stale))
        last_gap = random.Random(22).randbytes(1024)
        packed = bytes(start) + payload[:first_stop] + first_gap + payload[first_stop:second_stop] + last_gap + payload[second_stop:]
        third = start + second_stop + len(first_gap) + len(last_gap)
        mapfile = self.root / "disk.map"
        mapfile.write_text(f"0x0 + 1\n0 {start + first_stop} +\n{start + first_stop} {len(first_gap)} -\n"
                           f"{start + first_stop + len(first_gap)} {second_stop - first_stop} +\n"
                           f"{third - len(last_gap)} {len(last_gap)} ?\n{third} {len(payload) - second_stop} +\n")
        _, records = self.run_image(packed, metadata=False)
        self.assertIn(hashlib.sha256(stale).hexdigest(), [entry["sha256"] for entry in records])
        result, records = self.run_image(packed, metadata=False, deep=True, search_mib=1, mapfile=mapfile)
        self.assertEqual(result.unmapped_bytes, len(first_gap) + len(last_gap))
        recovered = next(entry for entry in records if entry["category"] == "assembled_jpeg")
        self.assertEqual(len(recovered["extents"]), 3)
        self.assertEqual((result.folder / recovered["file"]).read_bytes(), payload)
        self.assertNotIn(hashlib.sha256(stale).hexdigest(), [entry["sha256"] for entry in records])

    def test_rescue_map_input_output_delta_and_bad_ranges(self):
        png = photo(23, 20, "PNG")
        mapfile = self.root / "disk.map"
        mapfile.write_text(f"# file map\n0x0 + 1\n0x2000 {len(png)} +\n")
        result, _ = self.run_image(bytes(512) + png, metadata=False, mapfile=mapfile, map_delta=512 - 8192)
        self.assertEqual(result.decoded, 1)
        self.assertEqual(result.unmapped_bytes, 512)
        mapfile.write_text("0x0 + 1\n0x0 100 +\n90 10 -\n")
        with self.assertRaisesRegex(ValueError, "пересекающиеся"):
            self.run_image(png, mapfile=mapfile)

    def test_png_joins_crc_validated_fragments_in_either_disk_order(self):
        payload = photo(280, 280, "PNG")
        cursor = 8
        while payload[cursor + 4:cursor + 8] != b"IDAT":
            cursor += 12 + int.from_bytes(payload[cursor:cursor + 4], "big")
        split = (cursor + 12 + 65536 + 4096 + 511) // 512 * 512
        tail = payload[split:]
        for backwards in (False, True):
            with self.subTest(backwards=backwards):
                if backwards:
                    packed = tail + bytes(-len(tail) % 512) + payload[:split]
                else:
                    packed = payload[:split] + b"X" * 2048 + tail
                result, records = self.run_image(packed, metadata=False, deep=True, search_mib=1)
                saved = next(entry for entry in records if entry["category"] == "assembled_png")
                first, second = saved["extents"]
                self.assertEqual(first[0] > second[0], backwards)
                self.assertEqual((result.folder / saved["file"]).read_bytes(), payload)

    def test_invalid_files_and_cancellation(self):
        bad = b"noise" + b"\x89PNG\r\n\x1a\n" + bytes(1400)
        result, _ = self.run_image(bad)
        self.assertEqual(result.saved, 0)
        self.assertTrue(self.run_image(bad, cancelled=lambda: True)[0].cancelled)
        self.assertEqual(list(extract(photo(80, 80)[:300], lambda: False, repair_end=True)), [])


if __name__ == "__main__":
    unittest.main()
