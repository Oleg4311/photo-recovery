"""Sequential comparison with a frozen manifest and a shared wall budget."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil
from audit_benchmark import check_provenance, verified_outputs

ROOT = Path(__file__).resolve().parents[1]


def run(manifest_path, images, output, search_mib, engines=None):
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    settings = {"manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "wall_seconds": 120, "search_mib": search_mib, "cluster_size": 512,
                "engines": engines or ["0.3", "0.4", "PhotoRec-7.2-standard", "PhotoRec-7.2-bf"],
                "memory": "sum of process-tree RSS sampled every 20 ms; shared pages may be counted twice"}
    frozen = output / "settings.json"
    if frozen.exists() and json.loads(frozen.read_text()) != settings:
        raise ValueError("Campaign settings changed")
    frozen.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    results = []
    for engine in settings["engines"]:
        for case in manifest["images"]:
            image = images / case["name"]
            with image.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != case["sha256"]:
                    raise ValueError("Input checksum mismatch")
            dest = output / engine / image.stem
            dest.mkdir(parents=True, exist_ok=True)
            if engine in {"0.3", "0.4"}:
                src = ROOT / ("src" if engine == "0.4" else "benchmark/baseline-0.3/src")
                command = [str(ROOT / ".venv/Scripts/python.exe"), str(src / "cli.py"), str(image), str(dest),
                           "--no-metadata", "--deep", "--cluster-size", "512", "--search-mib", str(search_mib)]
            else:
                mode = "paranoid_bf" if engine.endswith("bf") else "paranoid"
                command = [str(ROOT / "benchmark/tools/testdisk-7.2/photorec_win.exe"),
                           "/debug", "/log", "/d", str(dest / "recovered"), "/cmd", str(image),
                           f"partition_none,options,{mode},keep_corrupted_file,fileopt,everything,disable,jpg,enable,png,enable,search"]
            started = time.perf_counter()
            peak = 0
            timed_out = False
            with (dest / "stdout.log").open("w") as stdout, (dest / "stderr.log").open("w") as stderr:
                process = subprocess.Popen(command, cwd=dest, stdout=stdout, stderr=stderr)
                monitor = psutil.Process(process.pid)
                while process.poll() is None:
                    try:
                        members = [monitor, *monitor.children(recursive=True)]
                        resident = 0
                        for member in members:
                            try:
                                resident += member.memory_info().rss
                            except psutil.NoSuchProcess:
                                pass
                        peak = max(peak, resident)
                    except psutil.NoSuchProcess:
                        pass
                    if time.perf_counter() - started > settings["wall_seconds"]:
                        timed_out = True
                        process.kill()
                        process.wait()
                        break
                    time.sleep(0.02)
            row = {"engine": engine, "image": image.name, "command": command,
                   "elapsed_seconds": time.perf_counter() - started, "peak_rss_bytes_sampled": peak,
                   "timeout": timed_out, "returncode": process.returncode}
            if engine in {"0.3", "0.4"}:
                folders = sorted(dest.glob("recovery-*"), key=lambda p: p.stat().st_mtime_ns)
                folder = folders[-1] if folders else dest
                row["provenance_checked"] = check_provenance(image, folder)
                events = [json.loads(line) for report in folder.glob("report.jsonl")
                          for line in report.read_text().splitlines()]
                saved = [e for e in events if e["event"] == "saved"]
                row["searches"] = [e for e in events if e["event"] == "fragment_search"]
                row["categories"] = {category: sum(e["category"] == category for e in saved)
                                     for category in sorted({e["category"] for e in saved})}
                ranks = {}
                for target in case["intact"]:
                    exact = next((e for e in saved if e["sha256"] == target["sha256"]), None)
                    if exact is None:
                        ranks[target["name"]] = None
                    else:
                        group = [e for e in saved if e["method"] == exact["method"]
                                 and e["image_offset"] == exact["image_offset"]]
                        ranks[target["name"]] = group.index(exact) + 1
                row["ranks"] = ranks
            else:
                folder = dest
                row["ranks"] = None  # Tool has no comparable assembly ranking API.
            row.update(verified_outputs(folder, case))
            results.append(row)
            (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(engine, image.name, "exact", len(row["exact"]), "timeout", timed_out, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("images", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--search-mib", type=int, default=1)
    parser.add_argument("--engines", nargs="+", choices=("0.3", "0.4", "PhotoRec-7.2-standard", "PhotoRec-7.2-bf"))
    args = parser.parse_args()
    run(args.manifest.resolve(), args.images.resolve(), args.output.resolve(), args.search_mib, args.engines)
