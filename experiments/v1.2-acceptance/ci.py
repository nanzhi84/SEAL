"""Fixed CI collection: real isolated CLI/PG paths plus offline artifact checks."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from archive_report import report

HERE = Path("experiments/v1.2-acceptance")
SUITES = {
    "core": [
        ("joint", [str(HERE / "acceptance.py")]),
        ("public-probe", [str(HERE / "acceptance.py"), "--only", "public-probe"]),
    ],
    "offline": [
        ("m3", ["experiments/v1.1-runtime-acceptance/acceptance.py", "--stage", "m3"]),
        ("url", [str(HERE / "url_acceptance.py")]),
        ("recheck", [str(HERE / "recheck_acceptance.py")]),
    ],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=SUITES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    groups, failed = {}, False
    for label, command in SUITES[args.group]:
        directory = args.output / label
        run = subprocess.run([sys.executable, *command, "--output", str(directory)])
        verifier = subprocess.run(
            [sys.executable, str(HERE / "verify_artifacts.py"), str(directory)],
            capture_output=True,
            text=True,
        )
        try:
            metadata = report(directory)
            status = (
                "PASS"
                if run.returncode == verifier.returncode == metadata["failed"] == 0
                else "FAIL"
            )
            groups[label] = {
                "status": status,
                **metadata,
                "offline_verification": json.loads(verifier.stdout),
            }
        except (OSError, ValueError, KeyError):
            status = "FAIL"
            groups[label] = {"status": status, "reason": "missing_or_invalid_acceptance_evidence"}
        failed |= status != "PASS"
    public = Path("artifacts/ci-public")
    public.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "FAIL" if failed else "PASS",
        "group": args.group,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "scope": "Synthetic CLI/isolated PostgreSQL/loopback; Replay and verification offline; no live source",
        "privacy": "Only names/statuses, hashes, environment and synthetic IDs; no raw bodies or field values",
        "groups": groups,
    }
    (public / (args.group + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"group": args.group, "status": result["status"]}))
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
