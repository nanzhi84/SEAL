"""Explicit, non-destructive import of a legacy file CAS into configured S3."""

import argparse
import json
from pathlib import Path

from seal.core import SealError, digest
from seal.objects import Objects


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path, help="Legacy SEAL_ARCHIVE directory (read-only)")
    args = parser.parse_args()
    root = args.archive / "objects"
    if not root.is_dir():
        raise SealError("legacy_archive_directory_required")
    objects = Objects()
    objects.check()
    count = 0
    for path in sorted(root.glob("*/*")):
        if not path.is_file():
            continue
        key = objects.key(path.parent.name + path.name)
        body = path.read_bytes()
        if digest(body) != key:
            raise SealError("legacy_archive_corrupt")
        if objects.put(body) != key or objects.get(key) != body:
            raise SealError("archive_import_verification_failed")
        count += 1
    print(json.dumps({"status": "verified", "objects": count, "source_deleted": False}))


if __name__ == "__main__":
    try:
        main()
    except SealError as exc:
        print(json.dumps({"error": exc.code}))
        raise SystemExit(2) from None
