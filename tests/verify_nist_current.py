"""Assert all 11 previously recovered NIST originals survive the current engine."""
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from recovery import scan
from audit_benchmark import check_provenance, verified_outputs

folder = ROOT / "benchmark/v05-nist"
folder.mkdir(exist_ok=True)
manifest = json.loads((ROOT / "benchmark/nist-graphic-manifest.json").read_text())
results = []
for definition in manifest["images"]:
    image = ROOT / "benchmark/nist-images" / definition["name"]
    with image.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == definition["sha256"]
    started = time.perf_counter()
    summary = scan(image, folder, metadata=False, deep=True, cluster_size=512, search_mib=16)
    result = verified_outputs(summary.folder, definition)
    result.update(image=image.name, provenance_checked=check_provenance(image, summary.folder),
                  seconds=time.perf_counter() - started, output=str(summary.folder))
    assert not result["missed"], result
    results.append(result)
    (folder / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(image.name, len(result["exact"]), "exact", flush=True)
assert sum(len(row["exact"]) for row in results) == 11
