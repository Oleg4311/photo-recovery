"""Create a synthetic disk image with known originals for a first run."""

import argparse
from pathlib import Path

from test_recovery import ntfs_image, photo, with_thumbnail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    folder = parser.parse_args().output
    folder.mkdir()
    originals = folder / "originals"
    originals.mkdir()
    jpeg = photo(120, 90)
    thumbnail = photo(24, 20)
    main_with_preview = with_thumbnail(photo(140, 100), thumbnail)
    (folder / "demo.img").write_bytes(ntfs_image(jpeg, main_with_preview[:-2]))
    (originals / "night.jpg").write_bytes(jpeg)
    (originals / "preview.jpg").write_bytes(thumbnail)
    (originals / "repaired.jpg").write_bytes(main_with_preview)
    print(folder / "demo.img")


if __name__ == "__main__":
    main()
