"""Compare exact photo recovery results against a folder of known originals."""

import argparse
import hashlib
from pathlib import Path

PHOTO_TYPES = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp"}


def photo_hashes(folder):
    if not folder.is_dir():
        raise ValueError(f"Folder does not exist: {folder}")
    hashes = {}
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() in PHOTO_TYPES and path.is_file():
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            hashes.setdefault(digest, []).append(path.relative_to(folder).as_posix())
    return hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("originals", type=Path)
    parser.add_argument("results", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        expected = photo_hashes(args.originals)
        if not expected:
            raise ValueError("No JPEG, PNG, or WebP originals were found.")
        for folder in args.results:
            actual = photo_hashes(folder)
            exact = expected.keys() & actual.keys()
            outside = actual.keys() - expected.keys()
            output_count = sum(map(len, actual.values()))
            print(f"{folder}: exact={len(exact)}/{len(expected)}, "
                  f"outside_reference={len(outside)}, duplicate_outputs={output_count - len(actual)}")
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
