"""Recompute artifact integrity and stored assertion outcomes without a database."""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory.resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    failures = []
    for name, expected in manifest["files"].items():
        path = (root / name).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            failures.append("artifact_hash_mismatch:" + name)
    assertions = json.loads((root / "assertions.json").read_text())
    for assertion in assertions:
        if assertion["status"] != "PASS" or assertion["expected"] != assertion["actual"]:
            failures.append("assertion:" + assertion["name"])
    print(
        json.dumps(
            {
                "verified_files": len(manifest["files"]),
                "assertions": len(assertions),
                "failures": failures,
                "scope": manifest["scope"],
                "unverified": manifest["unverified"],
            },
            ensure_ascii=False,
        )
    )
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
