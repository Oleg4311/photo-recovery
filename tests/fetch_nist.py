"""Download official NIST fixtures and require the previously frozen SHA-256."""
import bz2
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def fetch(row):
    folder = ROOT / "benchmark" / "nist-images"
    folder.mkdir(exist_ok=True)
    target = folder / row["name"]
    if not target.exists():
        url = "https://cfreds-archive.nist.gov/FileCarving/Images/" + row["name"] + ".bz2"
        compressed = target.with_suffix(".dd.bz2")
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=60) as response, compressed.open("wb") as output:
            shutil.copyfileobj(response, output)
        with bz2.open(compressed, "rb") as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
    with target.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != row["sha256"]:
        raise ValueError(f"SHA-256 mismatch: {target}")
    print(target.name, digest, flush=True)


if __name__ == "__main__":
    manifest = json.loads((ROOT / "benchmark/nist-graphic-manifest.json").read_text())
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fetch, manifest["images"]))
