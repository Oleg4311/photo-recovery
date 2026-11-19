"""Build and score a small reproducible set of difficult JPEG recovery cases."""

import argparse
import hashlib
import io
import json
import random
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from recovery import scan

from test_recovery import photo


def make_fixture(folder):
    folder.mkdir()
    originals = folder / "originals"
    originals.mkdir()
    rng = random.Random(94321)
    packed = bytearray()
    rows = []

    def add(name, payload, cluster, breaks, gaps, wanted=True):
        packed.extend(bytes(-len(packed) % 512))
        start = len(packed)
        previous = 0
        for boundary, gap in zip(breaks, gaps):
            packed.extend(payload[previous:boundary])
            packed.extend(gap)
            previous = boundary
        packed.extend(payload[previous:])
        if wanted:
            (originals / f"{name}.jpg").write_bytes(payload)
        rows.append({"name": name, "offset": start, "bytes": len(payload), "cluster": cluster,
                     "fragments": len(breaks) + 1, "expected_exact": wanted})
        pad_length = 64 * 1024
        packed.extend(bytes(pad_length))

    add("01-contiguous", photo(119, 89), 512, [], [])
    add("02-random-gap", photo(120, 90), 512, [2048], [rng.randbytes(3072)])
    add("03-text-gap", photo(137, 91), 512, [3072], [b"Lost bytes between fragments.   " * 64])
    add("04-false-marker", photo(146, 101), 512, [4096], [b"\xff\xd9" + rng.randbytes(1022)])
    restart = io.BytesIO()
    with Image.open(io.BytesIO(photo(128, 96))) as image:
        image.save(restart, "JPEG", quality=85, restart_marker_blocks=3)
    add("05-restart-markers", restart.getvalue(), 512, [4096], [b"\xff\xd9" + rng.randbytes(1022)])
    add("06-zero-gap", photo(155, 108), 512, [5120], [bytes(3584)])
    destroyed = photo(167, 103)
    destroyed = destroyed[:4096] + rng.randbytes(len(destroyed) - 4096)
    add("07-overwritten-tail", destroyed, 512, [], [], wanted=False)
    image_path = folder / "fragments.img"
    image_path.write_bytes(packed)
    (folder / "cases.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return image_path


def score(image, folder):
    expected = {hashlib.sha256(p.read_bytes()).hexdigest(): p.stem
                for p in (folder / "originals").glob("*.jpg")}
    measured = {}
    for name, deep in (("signature", False), ("deep", True)):
        started = time.monotonic()
        result = scan(image, folder, metadata=False, raw=True, deep=deep, search_mib=1)
        events = [json.loads(line) for line in (result.folder / "report.jsonl").read_text().splitlines()]
        found = {row["sha256"] for row in events if row["event"] == "saved"}
        unverified = sorted(
            row["category"]
            for row in events if row["event"] == "saved" and row["sha256"] not in expected
        )
        measured[name] = {
            "exact": len(found & expected.keys()),
            "expected": len(expected),
            "matched": sorted(expected[digest] for digest in found & expected.keys()),
            "outside_reference": unverified,
            "assembly_attempts": result.assembly_attempts,
            "assembly_capped": result.assembly_capped,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    return measured


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--generate-only", action="store_true")
    args = parser.parse_args()
    image = make_fixture(args.folder)
    if not args.generate_only:
        print(json.dumps(score(image, args.folder), indent=2))


if __name__ == "__main__":
    main()
