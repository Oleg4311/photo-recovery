"""Freeze fixtures before running; compare isolated engines with equal budgets.

python tests/benchmark_v04.py generate
python tests/benchmark_v04.py run
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import time

from PIL import Image, __version__ as pillow_version

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "benchmark" / "v04"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def generate():
    DATA.mkdir(exist_ok=True)
    manifest_path = DATA / "manifest.json"
    if manifest_path.exists():
        raise SystemExit("Manifest already frozen; use the existing fixtures.")
    rows = []
    for index, (width, height, fragments) in enumerate(((192, 144, 3), (211, 159, 3),
                                                       (227, 173, 4), (239, 181, 5))):
        rng = random.Random(810400 + index)
        stream = io.BytesIO()
        Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3)).save(
            stream, "JPEG", quality=86)
        payload = stream.getvalue()
        original = DATA / f"source-{index}.jpg"
        original.write_bytes(payload)
        packed = bytearray(bytes(1024))
        previous = 0
        extents = []
        for part in range(fragments):
            stop = len(payload) if part == fragments - 1 else 2048 + part * 3072
            extents.append([len(packed), stop - previous])
            packed.extend(payload[previous:stop])
            if part != fragments - 1:
                packed.extend(bytes((0 if part % 2 == 0 else 88,)) * (1024 + part * 512))
            previous = stop
        image = DATA / f"case-{index}.img"
        image.write_bytes(packed)
        rows.append({"name": image.name, "sha256": sha(image), "bytes": len(packed),
                     "group": f"synthetic-source-{index}", "split": "development" if index == 0 else "holdout",
                     "intact": [{"name": original.name, "sha256": sha(original)}],
                     "expected_extents": extents, "fragments": fragments})
    manifest_path.write_text(json.dumps({"generator_sha256": sha(Path(__file__)),
        "python": platform.python_version(), "pillow": pillow_version,
        "settings": {"deep": True, "metadata": False, "cluster_size": 512, "search_mib": 1},
        "budget": {"wall_seconds_per_image": 120, "max_candidates": 1500,
                   "max_work_blocks": 900000, "max_matches": 8}, "images": rows}, indent=2), encoding="utf-8")


def run():
    import psutil
    sys.path.insert(0, str(ROOT / "tests"))
    from audit_benchmark import check_provenance, verified_outputs
    manifest = json.loads((DATA / "manifest.json").read_text())
    results = []
    for engine, src in (("0.3", ROOT / "benchmark/baseline-0.3/src"), ("0.4", ROOT / "src")):
        for case in manifest["images"]:
            image = DATA / case["name"]
            if sha(image) != case["sha256"]:
                raise ValueError("Fixture changed after freeze")
            out = DATA / f"outputs-{engine}" / image.stem
            out.mkdir(parents=True, exist_ok=True)
            command = [str(ROOT / ".venv/Scripts/python.exe"), str(src / "cli.py"), str(image),
                       str(out), "--no-metadata", "--deep", "--cluster-size", "512", "--search-mib", "1"]
            started = time.perf_counter()
            peak = 0
            timed_out = False
            with (out / "stdout.log").open("w") as stdout, (out / "stderr.log").open("w") as stderr:
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
                monitor = psutil.Process(process.pid)
                while process.poll() is None:
                    try:
                        peak = max(peak, monitor.memory_info().rss)
                    except psutil.NoSuchProcess:
                        pass
                    if time.perf_counter() - started > manifest["budget"]["wall_seconds_per_image"]:
                        timed_out = True
                        process.kill()
                        process.wait()
                        break
                    time.sleep(0.02)
            row = {"engine": engine, "image": image.name, "split": case["split"],
                   "command": command, "elapsed_seconds": time.perf_counter() - started,
                   "peak_rss_bytes": peak, "memory_sampling_ms": 20,
                   "timeout": timed_out, "returncode": process.returncode}
            folders = sorted(out.glob("recovery-*"), key=lambda p: p.stat().st_mtime_ns)
            if folders:
                folder = folders[-1]
                row.update(verified_outputs(folder, case))
                row["provenance_checked"] = check_provenance(image, folder)
                events = [json.loads(line) for line in (folder / "report.jsonl").read_text().splitlines()]
                saved = [event for event in events if event["event"] == "saved"]
                candidates = [event for event in saved if event["method"] == "jpeg_fragments"]
                rank = next((i for i, event in enumerate(candidates, 1)
                             if event["sha256"] == case["intact"][0]["sha256"]), None)
                row.update({"exact_rank": rank, "top1": rank == 1, "top3": rank is not None and rank <= 3,
                            "alternatives": max(0, len(candidates) - 1),
                            "categories": {category: sum(e["category"] == category for e in saved)
                                           for category in sorted({e["category"] for e in saved})},
                            "searches": [e for e in events if e["event"] == "fragment_search"],
                            "finish": next((e for e in events if e["event"] == "finish"), None)})
            results.append(row)
            (DATA / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(engine, image.name, len(row.get("exact", [])), rank, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "run"))
    args = parser.parse_args()
    generate() if args.action == "generate" else run()
