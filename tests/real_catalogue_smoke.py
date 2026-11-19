"""Read only NTFS metadata for the user's reported Videos folder."""
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from catalog import build_catalogue

output = ROOT / "benchmark/v07-real-catalogue.json"
try:
    started = time.monotonic()
    result = build_catalogue("\\\\.\\C:", folder="Users/olegb/Videos")
    report = {"source": "C:", "folder": "Users/olegb/Videos", "read_only": True,
              "seconds": round(time.monotonic() - started, 2), "entries": len(result.entries),
              "deleted_files": sum(e.deleted and not e.directory for e in result.entries.values()),
              "warnings": result.warnings}
    assert not result.warnings, result.warnings
    assert all(e.directory or e.path.casefold().startswith("users/olegb/videos/") for e in result.entries.values())
    report["passed"] = True
except BaseException:
    report = {"passed": False, "error": traceback.format_exc()}
output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
sys.exit(0 if report["passed"] else 1)
