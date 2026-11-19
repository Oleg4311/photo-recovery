"""Download public Canon originals and their acquisition narrative."""
from pathlib import Path
import urllib.request
import zipfile

root = Path(__file__).resolve().parents[1] / "benchmark/canon-source"
root.mkdir(exist_ok=True)
for name in ("files-gen6.zip", "narrative.txt", "report.xml", "nps-2009-canon2-gen6.E01"):
    if (root / name).exists():
        continue
    url = "https://digitalcorpora.s3.amazonaws.com/corpora/drives/nps-2009-canon2/" + name
    with urllib.request.urlopen(url, timeout=60) as response:
        (root / name).write_bytes(response.read())
    print(name, (root / name).stat().st_size, flush=True)
with zipfile.ZipFile(root / "files-gen6.zip") as archive:
    for entry in archive.infolist():
        if not (root / entry.filename).resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsafe archive path")
    archive.extractall(root)
