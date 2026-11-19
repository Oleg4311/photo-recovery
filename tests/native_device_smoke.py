"""Exercise actual Windows device I/O on our own READ-ONLY attached test VHD.

Never initializes, formats, dismounts or writes any physical drive.
"""
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
from pathlib import Path
import struct
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from test_selected import fixture
from test_recovery import photo
from sources import ReadOnlyDevice, volume_disks
from catalog import build_catalogue
from recovery import scan


def run():
    if not C.windll.shell32.IsUserAnAdmin():
        raise PermissionError("Administrator token required for the disposable VHD test")
    folder = ROOT / "benchmark/v05-native"
    folder.mkdir(exist_ok=True)
    original = photo(192, 144)
    raw = fixture(original, broken=True)
    size = 16 * 1024 * 1024
    footer = bytearray(512)
    footer[:8] = b"conectix"
    struct.pack_into(">IIQ", footer, 8, 2, 0x10000, 0xffffffffffffffff)
    footer[28:32] = b"pr05"
    struct.pack_into(">I", footer, 32, 0x00050000)
    footer[36:40] = b"Wi2k"
    struct.pack_into(">QQHBBI", footer, 40, size, size, size // 512 // (16 * 63), 16, 63, 2)
    footer[68:84] = uuid.uuid4().bytes
    struct.pack_into(">I", footer, 64, (~sum(footer)) & 0xffffffff)
    vhd = folder / "disposable-read-only.vhd"
    with vhd.open("wb") as stream:
        stream.write(raw)
        stream.write(bytes(size - len(raw)))
        stream.write(footer)
    before = hashlib.sha256(vhd.read_bytes()).hexdigest()
    lib = C.WinDLL("virtdisk", use_last_error=True)
    class Storage(C.Structure):
        _fields_ = [("device", W.DWORD), ("vendor", C.c_ubyte * 16)]
    class Parameters(C.Structure):
        _fields_ = [("version", W.DWORD), ("value", W.DWORD)]
    storage = Storage(2, (C.c_ubyte * 16).from_buffer_copy(uuid.UUID("ec984aec-a0f9-47e9-901f-71415a66345b").bytes_le))
    lib.OpenVirtualDisk.argtypes = [C.POINTER(Storage), W.LPCWSTR, W.DWORD, W.DWORD, C.POINTER(Parameters), C.POINTER(W.HANDLE)]
    lib.AttachVirtualDisk.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, W.DWORD, C.POINTER(Parameters), C.c_void_p]
    lib.GetVirtualDiskPhysicalPath.argtypes = [W.HANDLE, C.POINTER(W.DWORD), W.LPWSTR]
    lib.DetachVirtualDisk.argtypes = [W.HANDLE, W.DWORD, W.DWORD]
    handle = W.HANDLE()
    error = lib.OpenVirtualDisk(C.byref(storage), str(vhd), 0x000D0000, 0, C.byref(Parameters(1, 1)), C.byref(handle))
    if error:
        raise C.WinError(error)
    attached = False
    try:
        error = lib.AttachVirtualDisk(handle, None, 3, 0, C.byref(Parameters(1, 0)), None)  # READ_ONLY | NO_DRIVE_LETTER
        if error:
            raise C.WinError(error)
        attached = True
        physical = C.create_unicode_buffer(1024)
        length = W.DWORD(C.sizeof(physical))
        error = lib.GetVirtualDiskPhysicalPath(handle, C.byref(length), physical)
        if error:
            raise C.WinError(error)
        with ReadOnlyDevice(physical.value) as device:
            assert device.size == size
            device.seek(511)
            assert device.read(1027) == raw[511:1538]
            try:
                device.write(b"never")
                raise AssertionError("A write operation was accepted")
            except OSError:
                pass
            device.guard_destination(folder)
        # Test the real destination mount-point mapping without reading its data.
        disk = next(iter(volume_disks(folder)))
        with ReadOnlyDevice(r"\\.\PhysicalDrive" + str(disk)) as actual:
            try:
                actual.guard_destination(folder)
                raise AssertionError("Same-source-disk output was accepted")
            except ValueError:
                pass
        catalogue = build_catalogue(physical.value)
        target = next(e for e in catalogue.entries.values() if e.name == "main.jpg")
        summary = scan(physical.value, folder, raw=False, selected_entries=[target.id], all_types=True, deep=True, search_mib=1)
        events = [json.loads(line) for line in (summary.folder / "report.jsonl").read_text().splitlines()]
        expected = hashlib.sha256(original).hexdigest()
        result = next(e for e in events if e.get("sha256") == expected)
        assert (summary.folder / result["file"]).read_bytes() == original
        assert hashlib.sha256(vhd.read_bytes()).hexdigest() == before
        evidence = {"physical_device": physical.value, "read_only_attachment": True,
                    "unaligned_read": "passed", "write_rejected": True,
                    "same_disk_output_rejected": True, "selected_file_exact_sha256": expected,
                    "source_vhd_unchanged_sha256": before, "output": str(summary.folder)}
        (folder / "result.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence, indent=2))
    finally:
        if attached:
            error = lib.DetachVirtualDisk(handle, 0, 0)
            if error:
                raise C.WinError(error)
        C.windll.kernel32.CloseHandle.argtypes = [W.HANDLE]
        C.windll.kernel32.CloseHandle(handle)


if __name__ == "__main__":
    try:
        run()
    except BaseException as error:
        folder = ROOT / "benchmark/v05-native"
        folder.mkdir(exist_ok=True)
        (folder / "error.txt").write_text(repr(error), encoding="utf-8")
        raise
