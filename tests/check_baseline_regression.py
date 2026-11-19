"""Run precisely the new unit-test fixture through the unmodified 0.3 engine."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark/baseline-0.3/src"))
from recovery import scan
sys.path.insert(0, str(ROOT / "tests"))
from test_recovery import photo

payload = photo(192, 144)
data = (bytes(1024) + payload[:2048] + bytes(1024)
        + payload[2048:6144] + b"X" * 1536 + payload[6144:])
path = ROOT / "benchmark/v04/unit-regression.img"
path.write_bytes(data)
summary = scan(path, path.parent, metadata=False, deep=True, search_mib=1)
events = [json.loads(line) for line in (summary.folder / "report.jsonl").read_text().splitlines()]
expected = hashlib.sha256(payload).hexdigest()
exact = any(e.get("sha256") == expected for e in events if e["event"] == "saved")
assert not exact, "0.3 unexpectedly recovered the new regression fixture"
(path.parent / "baseline-unit-regression.json").write_text(json.dumps({"engine": "0.3",
    "image_sha256": hashlib.sha256(data).hexdigest(), "expected_sha256": expected,
    "exact": exact, "output": str(summary.folder)}, indent=2), encoding="utf-8")
print("The new unit-test fixture fails exact recovery on frozen 0.3 as expected")
