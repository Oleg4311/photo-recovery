"""Read-only EWF conversion, official SHA-1 check, then freeze public test cases.

Uses the libewf DLL distributed in the official PhotoRec package. This is a
benchmark preparation tool, not additional application format support.
"""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "benchmark/canon-source"
OUT = ROOT / "benchmark/canon-v04"


def sha(path, algorithm="sha256"):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def convert():
    folder = (ROOT / "benchmark/tools/testdisk-7.2").resolve()
    cookie = os.add_dll_directory(str(folder))
    lib = C.CDLL(str(folder / "libewf-2.dll"))
    pointer = C.c_void_p
    lib.libewf_handle_initialize.argtypes = [C.POINTER(pointer), C.POINTER(pointer)]
    lib.libewf_handle_open.argtypes = [pointer, C.POINTER(C.c_char_p), C.c_int, C.c_int, C.POINTER(pointer)]
    lib.libewf_handle_read_buffer.argtypes = [pointer, pointer, C.c_size_t, C.POINTER(pointer)]
    lib.libewf_handle_read_buffer.restype = C.c_ssize_t
    lib.libewf_handle_close.argtypes = [pointer, C.POINTER(pointer)]
    lib.libewf_handle_free.argtypes = [C.POINTER(pointer), C.POINTER(pointer)]
    handle, error = pointer(), pointer()
    if lib.libewf_handle_initialize(C.byref(handle), C.byref(error)) != 1:
        raise RuntimeError("libewf initialization failed")
    paths = (C.c_char_p * 1)(os.fsencode(SOURCE / "nps-2009-canon2-gen6.E01"))
    target = SOURCE / "nps-2009-canon2-gen6.raw"
    try:
        if lib.libewf_handle_open(handle, paths, 1, lib.libewf_get_access_flags_read(), C.byref(error)) != 1:
            raise RuntimeError("libewf read-only open failed")
        buffer = C.create_string_buffer(1024 * 1024)
        with target.open("xb") as output:
            while True:
                count = lib.libewf_handle_read_buffer(handle, buffer, len(buffer), C.byref(error))
                if count < 0:
                    raise RuntimeError("libewf read failed")
                if count == 0:
                    break
                output.write(buffer.raw[:count])
    finally:
        lib.libewf_handle_close(handle, C.byref(error))
        lib.libewf_handle_free(C.byref(handle), C.byref(error))
        cookie.close()
    if sha(target, "sha1") != "4742c325f10583dab1eb4c55d0d45ab3beb99eb3":
        raise ValueError("Does not match the published raw-image SHA-1")
    return target


if __name__ == "__main__":
    raw = SOURCE / "nps-2009-canon2-gen6.raw"
    if not raw.exists():
        convert()
    if sha(raw, "sha1") != "4742c325f10583dab1eb4c55d0d45ab3beb99eb3":
        raise ValueError("Raw image failed official SHA-1")
    OUT.mkdir(exist_ok=True)
    manifest_path = OUT / "manifest.json"
    frozen = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if frozen and (frozen["ewf_sha256"] != sha(SOURCE / "nps-2009-canon2-gen6.E01")
                   or frozen["originals_zip_sha256"] != sha(SOURCE / "files-gen6.zip")):
        raise ValueError("Public source changed after freeze")
    originals = sorted(SOURCE.rglob("*.JPG"))
    rows = [{"name": raw.name, "sha256": sha(raw), "split": "public-holdout",
             "intact": [{"name": p.name, "sha256": sha(p)} for p in originals]}]
    for index, original in enumerate(originals[:3]):
        payload = original.read_bytes()
        image = OUT / f"public-three-pieces-{index}.img"
        image.write_bytes(payload[:65536] + bytes(4096) + payload[65536:131072]
                          + b"X" * 4096 + payload[131072:])
        rows.append({"name": image.name, "sha256": sha(image), "split": "public-derived-holdout",
                     "group": original.name, "intact": [{"name": original.name, "sha256": sha(original)}]})
    # Raw acquisition and derived cases share sources; all belong to the same
    # holdout partition. No Canon photo is used for tuning.
    manifest = {"source": "https://digitalcorpora.org/corpora/disk-images/",
                "camera": "Canon PowerShot SD800IS", "split": "all Canon sources held out",
                "raw_sha1_published": "4742c325f10583dab1eb4c55d0d45ab3beb99eb3",
                "ewf_sha256": sha(SOURCE / "nps-2009-canon2-gen6.E01"),
                "originals_zip_sha256": sha(SOURCE / "files-gen6.zip"), "images": rows}
    if frozen is not None:
        if frozen != manifest:
            raise ValueError("Regenerated fixtures differ from the frozen manifest")
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # Campaign expects one directory containing every image.
    import shutil
    shutil.copyfile(raw, OUT / raw.name)
    print(len(originals), "public originals; manifest frozen", flush=True)
