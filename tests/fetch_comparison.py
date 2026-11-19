"""Fetch the official portable PhotoRec package and public-corpus index."""
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1] / "benchmark" / "tools"
ROOT.mkdir(exist_ok=True)
for name, url in (
    ("testdisk-7.2.win64.zip", "https://www.cgsecurity.org/testdisk-7.2.win64.zip"),
    ("canon2-index.xml", "https://digitalcorpora.s3.amazonaws.com/?list-type=2&prefix=corpora/drives/nps-2009-canon2/"),
):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            (ROOT / name).write_bytes(response.read())
        print(name, (ROOT / name).stat().st_size, flush=True)
        if name.endswith(".zip"):
            with zipfile.ZipFile(ROOT / name) as archive:
                for entry in archive.infolist():
                    target = (ROOT / entry.filename).resolve()
                    if not target.is_relative_to(ROOT.resolve()):
                        raise ValueError("Unsafe archive path")
                archive.extractall(ROOT)
    except Exception as error:
        (ROOT / (name + ".error.txt")).write_text(str(error), encoding="utf-8")
        print(name, str(error), flush=True)
