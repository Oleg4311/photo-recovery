"""Audit recovery outputs against a per-image manifest of original SHA-256 hashes."""

import argparse
import hashlib
import json
from pathlib import Path

TARGETS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp"}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_outputs(folder, definition):
    known = {target["sha256"]: target["name"] for target in definition["intact"]}
    seen = set()
    duplicates = 0
    rows = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TARGETS:
            continue
        digest = sha256(path)
        duplicates += digest in seen
        seen.add(digest)
        rows.append({"file": path.relative_to(folder).as_posix(), "bytes": path.stat().st_size,
                     "sha256": digest, "match": known.get(digest)})
    exact = sorted(known[digest] for digest in seen & known.keys())
    return {"exact": exact, "missed": sorted(set(known.values()) - set(exact)),
            "unmatched_unique": len(seen - known.keys()), "duplicate_outputs": duplicates,
            "outputs": rows}


def check_provenance(image, folder):
    reports = list(folder.rglob("report.jsonl"))
    checked = 0
    with image.open("rb") as stream:
        for report in reports:
            with report.open(encoding="utf-8") as records:
                for line in records:
                    row = json.loads(line)
                    if row["event"] != "saved" or row.get("method") not in {"jpeg_fragments", "png_fragments", "zip_fragments"}:
                        continue
                    digest = hashlib.sha256()
                    for offset, length in row["extents"]:
                        if not (0 <= offset < offset + length <= image.stat().st_size):
                            raise ValueError("Reported extent is outside the image.")
                        stream.seek(offset)
                        remaining = length
                        while remaining:
                            block = stream.read(min(remaining, 1024 * 1024))
                            if not block:
                                raise ValueError("Reported extent exceeds readable data.")
                            digest.update(block)
                            remaining -= len(block)
                    if digest.hexdigest() != row["sha256"] or sha256(report.parent / row["file"]) != row["sha256"]:
                        raise ValueError(f'Output does not match its reported physical extents: {row["file"]}')
                    checked += 1
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("image", type=Path)
    parser.add_argument("results", type=Path)
    parser.add_argument("--check-extents", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    try:
        if not args.results.is_dir():
            raise ValueError("Provide a completed run's output directory.")
        digest = sha256(args.image)
        matching = [item for item in manifest["images"] if item["sha256"] == digest]
        if not matching:
            raise ValueError("The SHA-256 of the input image is absent from the manifest.")
        definition, = matching
        score = verified_outputs(args.results, definition)
        score["image"] = definition["name"]
        score["image_sha256"] = digest
        if args.check_extents:
            score["provenance_checked"] = check_provenance(args.image, args.results)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(score, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
