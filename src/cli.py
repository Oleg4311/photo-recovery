"""Command-line access to the same recovery engine as the desktop UI."""

import argparse
import sys
import json
from pathlib import Path

from recovery import scan
from catalog import build_catalogue
from sources import list_devices


def main():
    parser = argparse.ArgumentParser(description="Recover photos from a read-only raw disk image or a folder of copies.")
    parser.add_argument("--version", action="version", version="Photo Recovery 0.7")
    parser.add_argument("image", nargs="?", type=Path)
    parser.add_argument("destination", nargs="?", type=Path)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--list-deleted", action="store_true", help="list deleted files and folders with stable selection IDs")
    parser.add_argument("--select", action="append", help="recover only this file/folder ID, repeatable; use --deep for targeted assembly")
    parser.add_argument("--all-types", action="store_true", help="also extract other file types from filesystem records")
    parser.add_argument("--copies", type=Path, help="search an additional folder for existing photo files")
    parser.add_argument("--max-mib", type=int, default=64)
    parser.add_argument("--no-metadata", action="store_true")
    parser.add_argument("--no-signatures", action="store_true")
    parser.add_argument("--deep", action="store_true", help="try JPEG and PNG fragment assembly")
    parser.add_argument("--cluster-size", type=int, default=512)
    parser.add_argument("--search-mib", type=int, default=2)
    parser.add_argument("--mapfile", type=Path, help="GNU ddrescue map for the supplied image")
    parser.add_argument("--map-delta", type=int, default=0, help="output position minus input position, in bytes")
    args = parser.parse_args()
    try:
        if args.list_devices:
            print(json.dumps(list_devices(), ensure_ascii=True, indent=2))
            return
        if args.list_deleted:
            if args.image is None:
                parser.error("Provide an image or device for --list-deleted")
            catalogue = build_catalogue(args.image)
            print(json.dumps({"source": catalogue.source, "entries": catalogue.rows(), "warnings": catalogue.warnings}, ensure_ascii=True, indent=2))
            return
        if args.copies and args.destination is None:
            args.destination, args.image = args.image, None
        if args.destination is None:
            parser.error("Provide an existing destination directory")
        result = scan(args.image, args.destination, args.max_mib, metadata=not args.no_metadata,
                      raw=not args.no_signatures and not args.select, copies=args.copies, deep=args.deep,
                      cluster_size=args.cluster_size, search_mib=args.search_mib,
                      mapfile=args.mapfile, map_delta=args.map_delta,
                      selected_entries=args.select, all_types=args.all_types)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    except KeyboardInterrupt:
        parser.exit(130)
    print(result.folder)
    print(f"files={result.files} decoded={result.decoded} repaired={result.repaired} partial={result.partial} thumbnails={result.thumbnails} "
          f"assembled={result.assembled} ambiguous={result.ambiguous} attempts={result.assembly_attempts} "
          f"searches_capped={result.assembly_capped} unmapped_bytes={result.unmapped_bytes} "
          f"cache_previews={result.cache_previews} duplicates={result.duplicates} volumes={result.volumes} errors={result.failed_volumes}")


if __name__ == "__main__":
    sys.exit(main())
