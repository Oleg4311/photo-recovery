"""Read-only Windows disk access. No write access, locking or dismount IOCTLs."""
import ctypes as C
from ctypes import wintypes as W
import io
import json
import os
from pathlib import Path
import re
import subprocess

DEVICE = re.compile(r"^\\\\\.\\(?:PhysicalDrive[0-9]+|[A-Za-z]:)$", re.I)


def is_device(source):
    return source is not None and DEVICE.fullmatch(str(source)) is not None


def _kernel():
    if os.name != "nt":
        raise OSError("Прямое чтение дисков поддерживается только в Windows.")
    k = C.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    k.CreateFileW.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
    k.SetFilePointerEx.argtypes = [W.HANDLE, C.c_longlong, C.POINTER(C.c_longlong), W.DWORD]
    k.ReadFile.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
    return k


def _open(k, path, access):
    handle = k.CreateFileW(str(path), access, 3, None, 3, 0, None)
    if handle == C.c_void_p(-1).value:
        error = C.get_last_error()
        if error == 5:
            raise PermissionError("Windows запретила чтение диска. Нажмите «Доступ к дискам: администратор».")
        raise C.WinError(error)
    return handle


def _ioctl(k, handle, code, size=65536):
    buffer = C.create_string_buffer(size)
    returned = W.DWORD()
    if not k.DeviceIoControl(handle, code, None, 0, buffer, size, C.byref(returned), None):
        raise C.WinError(C.get_last_error())
    return buffer.raw[:returned.value]


def volume_disks(path):
    """Resolve an existing destination through mount points; fail closed."""
    k = _kernel()
    mount = C.create_unicode_buffer(32768)
    k.GetVolumePathNameW.argtypes = [W.LPCWSTR, W.LPWSTR, W.DWORD]
    k.GetDriveTypeW.argtypes = [W.LPCWSTR]
    k.GetVolumeNameForVolumeMountPointW.argtypes = [W.LPCWSTR, W.LPWSTR, W.DWORD]
    if not k.GetVolumePathNameW(str(Path(path).resolve(strict=True)), mount, len(mount)):
        raise C.WinError(C.get_last_error())
    if k.GetDriveTypeW(mount.value) == 4:  # Positively identified remote volume.
        return set()
    volume = C.create_unicode_buffer(1024)
    if not k.GetVolumeNameForVolumeMountPointW(mount.value, volume, len(volume)):
        raise C.WinError(C.get_last_error())
    handle = _open(k, volume.value.rstrip("\\"), 0)
    try:
        data = _ioctl(k, handle, 0x00560000)  # IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS
        count = int.from_bytes(data[:4], "little")
        if not 0 < count <= 1024 or len(data) < 8 + count * 24:
            raise OSError("Не удалось определить физические диски папки назначения.")
        return {int.from_bytes(data[8 + index * 24:12 + index * 24], "little") for index in range(count)}
    finally:
        k.CloseHandle(handle)


def reject_same_disk(source_disks, destination_disks):
    if not source_disks:
        raise ValueError("Не удалось определить исходный физический диск.")
    if source_disks & destination_disks:
        raise ValueError("Папка результатов находится на исходном физическом диске. Выберите другой накопитель.")


class ReadOnlyDevice:
    def __init__(self, path):
        if not is_device(path):
            raise ValueError("Недопустимое имя диска или раздела.")
        self.k = _kernel()
        self.handle = _open(self.k, path, 0x80000000)  # GENERIC_READ, never GENERIC_WRITE
        self.position = 0
        self.path = str(path)
        try:
            self.size = int.from_bytes(_ioctl(self.k, self.handle, 0x0007405C, 8), "little")
            geometry = _ioctl(self.k, self.handle, 0x00070000, 24)
            self.sector = int.from_bytes(geometry[20:24], "little")
            if not self.size or self.sector not in (512, 1024, 2048, 4096):
                raise OSError("Неподдерживаемая геометрия накопителя.")
            if "physicaldrive" in self.path.lower():
                number = _ioctl(self.k, self.handle, 0x002D1080, 12)
                self.disks = {int.from_bytes(number[4:8], "little")}
                if self.disks != {int(self.path.lower().split("physicaldrive")[1])}:
                    raise OSError("Идентификатор физического диска изменился.")
            else:
                data = _ioctl(self.k, self.handle, 0x00560000)
                count = int.from_bytes(data[:4], "little")
                if not 0 < count <= 1024 or len(data) < 8 + count * 24:
                    raise OSError("Неподдерживаемое размещение раздела.")
                self.disks = {int.from_bytes(data[8 + i * 24:12 + i * 24], "little") for i in range(count)}
        except BaseException:
            self.close()
            raise

    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if position < 0:
            raise ValueError("Отрицательное смещение.")
        self.position = position
        return position

    def tell(self):
        return self.position

    def read(self, count=-1):
        if count < 0:
            raise ValueError("Для устройства требуется ограниченный размер чтения.")
        count = min(count, max(0, self.size - self.position))
        if not count:
            return b""
        low = self.position // self.sector * self.sector
        high = min(self.size, ((self.position + count + self.sector - 1) // self.sector) * self.sector)
        if high - low > 257 * 1024 * 1024:
            raise ValueError("Слишком большой запрос чтения устройства.")
        if not self.k.SetFilePointerEx(self.handle, low, None, 0):
            raise C.WinError(C.get_last_error())
        buffer = C.create_string_buffer(high - low + 65536)
        address = (C.addressof(buffer) + 65535) & ~65535
        got = W.DWORD()
        if not self.k.ReadFile(self.handle, C.c_void_p(address), high - low, C.byref(got), None):
            raise C.WinError(C.get_last_error())
        if got.value != high - low:
            raise OSError(f"Неполное чтение диска около 0x{low:X}.")
        result = C.string_at(address + self.position - low, count)
        self.position += len(result)
        return result

    def write(self, *_):
        raise io.UnsupportedOperation("Устройство открыто только для чтения.")

    def close(self):
        if self.handle is not None:
            self.k.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def guard_destination(self, destination):
        reject_same_disk(self.disks, volume_disks(destination))


def list_devices():
    if os.name != "nt":
        return []
    script = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$rows=@()
foreach($d in Get-CimInstance Win32_DiskDrive){
 $rows+=@{path=('\\.\PhysicalDrive'+$d.Index);label=('Диск '+$d.Index+' · '+$d.Model+' · '+[math]::Round([double]$d.Size/1GB,1)+' ГБ')}
}
foreach($v in Get-CimInstance Win32_LogicalDisk){
 if($v.DriveType -in 2,3){$rows+=@{path=('\\.\'+$v.DeviceID);label=($v.DeviceID+' '+$v.VolumeName+' · '+$v.FileSystem+' · '+[math]::Round([double]$v.Size/1GB,1)+' ГБ')}}
}
ConvertTo-Json -InputObject @($rows) -Compress
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, timeout=40, creationflags=0x08000000)
    if result.returncode:
        raise OSError("Не удалось получить список дисков Windows.")
    return json.loads(result.stdout.decode("utf-8-sig"))


def open_source(source):
    return ReadOnlyDevice(source) if is_device(source) else Path(source).open("rb")


def source_size(stream):
    return stream.size if isinstance(stream, ReadOnlyDevice) else os.fstat(stream.fileno()).st_size
