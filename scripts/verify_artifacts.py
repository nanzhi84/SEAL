"""Recompute artifact integrity and stored assertion outcomes without a database."""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--recorded-outcomes",
        action="store_true",
        help="Verify evidence and truthful PASS/FAIL recording, allowing documented failed assertions",
    )
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
    failed_assertions = []
    for assertion in assertions:
        equal = assertion["expected"] == assertion["actual"]
        recorded = "PASS" if equal else "FAIL"
        if assertion["status"] == "FAIL":
            failed_assertions.append(assertion["name"])
        if assertion["status"] != recorded or (not args.recorded_outcomes and not equal):
            failures.append("assertion:" + assertion["name"])
    print(
        json.dumps(
            {
                "verified_files": len(manifest["files"]),
                "assertions": len(assertions),
                "failures": failures,
                "failed_assertions": failed_assertions,
                "mode": "recorded_outcomes" if args.recorded_outcomes else "require_pass",
                "scope": manifest["scope"],
                "unverified": manifest["unverified"],
            },
            ensure_ascii=False,
        )
    )
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
