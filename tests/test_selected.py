import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import random

from test_recovery import photo, filename, mft_record, ntfs_image, nonresident, resident, put, flash_image
from catalog import build_catalogue
from recovery import scan
from sources import is_device, reject_same_disk
from targeted import valid_zip
from ntfs import Volume


def named(name, parent, sequence):
    attr = bytearray(filename(name))
    put(attr, 24, parent | (sequence << 48), 8)
    return attr


def entry(name, parent, parent_sequence, sequence, directory=False, active=False, data=None):
    attrs = [named(name, parent, parent_sequence)]
    if data is not None:
        attrs.append(data)
    record = bytearray(mft_record(active, *attrs))
    put(record, 16, sequence, 2)
    put(record, 22, int(active) | (2 if directory else 0), 2)
    return record


def fixture(payload, name="main.jpg", broken=False):
    packed = ntfs_image(payload or b"placeholder", photo(24, 24))
    def install(number, record):
        offset = (4 + number * 2) * 512 if number < 10 else (26 + (number - 10) * 2) * 512
        packed[offset:offset + 1024] = record
    install(5, entry(".", 5, 1, 1, True, True))
    install(10, entry("Pictures", 5, 1, 3, True, True))
    install(11, entry("Deleted", 10, 3, 7, True))
    install(12, entry("Other", 5, 1, 9, True))
    if broken:
        data = nonresident([(100, math.ceil(len(payload) / 512))], len(payload))
        physical = payload[:2048] + bytes(1024) + payload[2048:6144] + b"X" * 1536 + payload[6144:]
        packed[100 * 512:100 * 512 + len(physical)] = physical
    else:
        data = resident(0x80, payload)
    install(25, entry(name, 11, 7, 2, data=data))
    install(26, entry("unrelated.txt", 12, 9, 4, data=resident(0x80, b"Do not recover this file")))
    return packed


class SelectedTests(unittest.TestCase):
    def test_catalogue_limit_keeps_partial_records_and_scoped_search_reaches_late_file(self):
        data = fixture(b"target", "notes.dat")
        for number in range(13, 25):
            offset = (26 + (number - 10) * 2) * 512
            data[offset:offset + 1024] = entry(f"noise-{number}.dat", 12, 9, 1, data=resident(0x80, b"noise"))
        self.image.write_bytes(data)
        with patch("catalog.MAX_NODES", 10):
            partial = build_catalogue(self.image)
            self.assertTrue(partial.entries)
            self.assertTrue(partial.warnings)
            self.assertTrue(any(e.name.startswith("noise-") for e in partial.entries.values()))
            scoped = build_catalogue(self.image, folder="Pictures/Deleted")
            files = [e for e in scoped.entries.values() if e.deleted and not e.directory]
            self.assertEqual([e.name for e in files], ["notes.dat"])
            self.assertFalse(scoped.warnings)
            target = files[0]
            selected = build_catalogue(self.image, selected={target.id})
            self.assertEqual([e.id for e in selected.entries.values() if e.deleted and not e.directory], [target.id])
            for number in range(13, 25):
                offset = (26 + (number - 10) * 2) * 512
                data[offset:offset + 1024] = entry(f"unrelated-directory-{number}", 5, 1, 1, True, True)
            self.image.write_bytes(data)
            scoped = build_catalogue(self.image, folder="Pictures/Deleted")
            self.assertFalse(scoped.warnings, "Unrelated directories must not consume the scoped limit")
            self.assertEqual([e.name for e in scoped.entries.values() if e.deleted and not e.directory], ["notes.dat"])

    def test_scoped_catalogue_rejects_reused_parent_and_retains_cancelled_results(self):
        self.image.write_bytes(fixture(b"target", "notes.dat"))
        scoped = build_catalogue(self.image, folder="Other")
        self.assertEqual([e.name for e in scoped.entries.values() if e.deleted and not e.directory], ["unrelated.txt"])
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            return calls > 23
        partial = build_catalogue(self.image, cancelled=cancelled)
        self.assertTrue(partial.cancelled)
        self.assertTrue(partial.entries)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "test.img"

    def recover(self, data, selection, **kwargs):
        self.image.write_bytes(data)
        catalogue = build_catalogue(self.image)
        chosen = next(e for e in catalogue.entries.values() if e.name == selection)
        before = hashlib.sha256(data).hexdigest()
        with patch("recovery.hits", side_effect=AssertionError("Whole-disk scan must not run")):
            summary = scan(self.image, self.root, metadata=True, raw=False, selected_entries=[chosen.id],
                           all_types=True, search_mib=1, **kwargs)
        self.assertEqual(hashlib.sha256(self.image.read_bytes()).hexdigest(), before)
        rows = [json.loads(line) for line in (summary.folder / "report.jsonl").read_text().splitlines()]
        return catalogue, summary, rows

    def test_deleted_folder_and_generic_file_preserve_path_and_scope(self):
        payload = b"arbitrary non-image file\x00\x01\x02"
        catalogue, summary, rows = self.recover(fixture(payload, "notes.dat"), "Deleted")
        folder = next(e for e in catalogue.entries.values() if e.name == "Deleted")
        self.assertTrue(folder.deleted)
        self.assertEqual(folder.children, 1)
        saved = [r for r in rows if r["event"] == "saved"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["original_path"], "Pictures/Deleted/notes.dat")
        self.assertEqual(saved[0]["integrity"], "not_verified")
        self.assertEqual((summary.folder / saved[0]["file"]).read_bytes(), payload)

    def test_one_file_deep_recovers_missing_fragments_without_other_files(self):
        payload = photo(192, 144)
        data = fixture(payload, broken=True)
        _, shallow, rows = self.recover(data, "main.jpg")
        self.assertNotIn(hashlib.sha256(payload).hexdigest(), [r.get("sha256") for r in rows])
        _, deep, rows = self.recover(data, "main.jpg", deep=True)
        result = next(r for r in rows if r.get("sha256") == hashlib.sha256(payload).hexdigest())
        self.assertEqual(len(result["extents"]), 3)
        self.assertEqual((deep.folder / result["file"]).read_bytes(), payload)
        self.assertTrue(all(r["original_name"] == "main.jpg" for r in rows if r["event"] == "saved"))
        with self.image.open("rb") as image:
            assembled = b""
            for offset, length in result["extents"]:
                image.seek(offset)
                assembled += image.read(length)
        self.assertEqual(assembled, payload)

    def test_deep_selected_office_zip_checks_all_crcs(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("word/document.xml", random.Random(731).randbytes(10000))
        payload = stream.getvalue()
        self.assertTrue(valid_zip(payload, lambda: False))
        _, summary, rows = self.recover(fixture(payload, "document.docx", True), "document.docx", deep=True)
        found = next(r for r in rows if r.get("sha256") == hashlib.sha256(payload).hexdigest())
        self.assertEqual(found["category"], "assembled_file")
        self.assertEqual(found["integrity"], "zip_crc")
        self.assertEqual((summary.folder / found["file"]).read_bytes(), payload)

    def test_reused_parent_record_is_not_a_false_folder_membership(self):
        data = fixture(b"data", "notes.dat")
        # Same parent record number, different NTFS sequence: it is another folder.
        position = (26 + (11 - 10) * 2) * 512
        data[position:position + 1024] = entry("Reused", 10, 3, 8, True, True)
        self.image.write_bytes(data)
        catalogue = build_catalogue(self.image)
        file = next(e for e in catalogue.entries.values() if e.name == "notes.dat")
        folder = next(e for e in catalogue.entries.values() if e.name == "Reused")
        self.assertFalse(file.path_known)
        self.assertEqual(catalogue.targets([folder.id]), {})

    def test_deleted_fat_directory_is_visible_and_selectable(self):
        payload = photo(24, 22, "PNG")
        data = flash_image(payload, False)
        child = data[1024:1056]
        data[2048:2080] = child
        directory = bytearray(32)
        directory[:11] = b"\xe5LDPICS    "
        directory[11] = 16
        put(directory, 26, 4, 2)
        data[1024:1056] = directory
        _, summary, rows = self.recover(data, "_LDPICS")
        found = next(r for r in rows if r.get("sha256") == hashlib.sha256(payload).hexdigest())
        self.assertTrue(found["original_path"].startswith("_LDPICS/"))

    def test_deleted_exfat_directory_is_visible_and_selectable(self):
        payload = photo(24, 22, "PNG")
        data = flash_image(payload, True)
        data[2048:2144] = data[1056:1152]
        first, stream, name = bytearray(32), bytearray(32), bytearray(32)
        first[:2] = bytes((0x85, 2))
        put(first, 4, 16, 2)
        label = "Удалено"
        stream[:4] = bytes((0xc0, 3, 0, len(label)))
        put(stream, 8, 512, 8)
        put(stream, 20, 4, 4)
        put(stream, 24, 512, 8)
        name[0] = 0xc1
        encoded = label.encode("utf-16-le")
        name[2:2 + len(encoded)] = encoded
        checksum = 0
        for number, record in enumerate((first, stream, name)):
            for index, value in enumerate(record):
                if number == 0 and index in (2, 3):
                    continue
                checksum = (((checksum >> 1) | (checksum << 15)) + value) & 0xffff
        put(first, 2, checksum, 2)
        for record in (first, stream, name):
            record[0] &= 0x7f
        data[1056:1152] = first + stream + name
        _, summary, rows = self.recover(data, label)
        found = next(r for r in rows if r.get("sha256") == hashlib.sha256(payload).hexdigest())
        self.assertEqual(found["original_path"], "Удалено/Карта.png")

    def test_invalid_selection_cannot_fall_back_to_whole_disk(self):
        self.image.write_bytes(fixture(b"data", "notes.dat"))
        with self.assertRaises(ValueError):
            scan(self.image, self.root, selected_entries=["does-not-exist"], raw=False)
        with self.assertRaises(ValueError):
            scan(self.image, self.root, selected_entries=[], raw=False)
        with self.assertRaises(ValueError):
            scan(self.image, self.root, selected_entries=["id"], raw=True)

    def test_device_names_and_same_physical_disk_guard(self):
        self.assertTrue(is_device(r"\\.\PhysicalDrive12"))
        self.assertTrue(is_device(r"\\.\E:"))
        self.assertFalse(is_device(r"\\.\PhysicalDrive0\evil"))
        self.assertFalse(is_device(r"\\?\GLOBALROOT\Device\Harddisk0"))
        with self.assertRaises(ValueError):
            reject_same_disk({1}, {1, 2})
        with self.assertRaises(ValueError):
            reject_same_disk(set(), {2})
        reject_same_disk({1}, {2})

    def test_large_generic_file_streams_in_bounded_chunks_and_can_cancel(self):
        payload = random.Random(121).randbytes(2100 * 512)
        data = fixture(b"placeholder", "large.bin")
        data.extend(bytes(8 * 1024 * 1024 - len(data)))
        put(data, 40, len(data) // 512, 8)
        runs = [(100 + index * 200, 200) for index in range(11)]
        record = entry("large.bin", 11, 7, 2, data=nonresident(runs, len(payload)))
        location = (26 + 15 * 2) * 512
        data[location:location + 1024] = record
        data[100 * 512:100 * 512 + len(payload)] = payload
        original = Volume.read_stream
        sizes = []
        def read(volume, extents, offset, count):
            sizes.append(count)
            return original(volume, extents, offset, count)
        with patch.object(Volume, "read_stream", read):
            _, summary, rows = self.recover(data, "large.bin")
        self.assertLessEqual(max(sizes), 1024 * 1024)
        saved = next(r for r in rows if r["event"] == "saved")
        self.assertEqual((summary.folder / saved["file"]).read_bytes(), payload)
        catalogue = build_catalogue(self.image)
        target = next(e.id for e in catalogue.entries.values() if e.name == "large.bin")
        stop = [False]
        def progress(stage, *_):
            if stage.startswith("Извлечение"):
                stop[0] = True
        summary = scan(self.image, self.root, raw=False, all_types=True, selected_entries=[target],
                       progress=progress, cancelled=lambda: stop[0])
        self.assertTrue(summary.cancelled)
        self.assertEqual(summary.files, 0)
        self.assertEqual(list(summary.folder.rglob("*.part")), [])

    def test_empty_generic_file_is_recovered(self):
        _, summary, rows = self.recover(fixture(b"", "empty.txt"), "empty.txt")
        saved = next(r for r in rows if r["event"] == "saved")
        self.assertEqual((summary.folder / saved["file"]).read_bytes(), b"")
