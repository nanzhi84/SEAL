"""Fixed offline V1.3 collection; only synthetic metadata is published."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    suites = [
        (
            "unit",
            [
                "-m",
                "unittest",
                "discover",
                "-s",
                "experiments/v1.3-acceptance",
                "-p",
                "test_*.py",
                "-v",
            ],
        ),
        (
            "discovery",
            [
                "experiments/v1.3-acceptance/discovery_acceptance.py",
                "--output",
                str(args.output / "discovery"),
            ],
        ),
        (
            "persistence",
            [
                "experiments/v1.3-acceptance/persistence_acceptance.py",
                "--output",
                str(args.output / "persistence"),
            ],
        ),
    ]
    results = {}
    for name, command in suites:
        with (args.output / (name + ".log")).open("w") as log:
            run = subprocess.run([sys.executable, *command], stdout=log, stderr=log, check=False)
        value = {"exit": run.returncode, "status": "PASS" if run.returncode == 0 else "FAIL"}
        assertions = args.output / name / "assertions.json"
        if assertions.exists():
            checks = json.loads(assertions.read_text())
            value.update(assertions=len(checks), failed=sum(c["status"] != "PASS" for c in checks))
            if value["failed"]:
                value["status"] = "FAIL"
            verification = subprocess.run(
                [
                    sys.executable,
                    "experiments/v1.2-acceptance/verify_artifacts.py",
                    str(args.output / name),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            value["offline_verification_exit"] = verification.returncode
            try:
                value["offline_verification"] = json.loads(verification.stdout)
            except ValueError:
                value["offline_verification"] = {"reason": "missing_or_invalid_evidence"}
            if verification.returncode:
                value["status"] = "FAIL"
        results[name] = value
    summary = {
        "status": "PASS" if all(v["status"] == "PASS" for v in results.values()) else "FAIL",
        "group": "seeded",
        "suites": results,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "scope": "Controlled loopback, PostgreSQL and offline Replay; public sites excluded",
        "engine_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path("src/seal").rglob("*"))
            if p.is_file() and p.suffix in (".py", ".sql")
        },
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    public = Path("artifacts/ci-public")
    public.mkdir(parents=True, exist_ok=True)
    (public / "seeded.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    return int(summary["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
