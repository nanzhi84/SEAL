"""Fixed document collection and offline oracle; publish metadata only."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    commands = {
        "unit": [
            "-m",
            "unittest",
            "discover",
            "-s",
            "experiments/document-pipeline",
            "-p",
            "test_*.py",
            "-v",
        ],
        "acceptance": [
            "experiments/document-pipeline/acceptance.py",
            "--output",
            str(args.output / "acceptance"),
        ],
        "verification": [
            "experiments/document-pipeline/verify.py",
            str(args.output / "acceptance"),
        ],
    }
    results = {}
    for name, command in commands.items():
        with (args.output / (name + ".log")).open("w") as log:
            run = subprocess.run([sys.executable, *command], stdout=log, stderr=log, check=False)
        value = {"exit": run.returncode, "status": "PASS" if run.returncode == 0 else "FAIL"}
        if name == "unit":
            count = re.search(r"Ran (\d+) tests in", (args.output / "unit.log").read_text())
            value["tests"] = int(count.group(1)) if count else 0
            if not value["tests"]:
                value["status"] = "FAIL"
        if name == "verification":
            try:
                value["independent_oracle"] = json.loads(
                    (args.output / "verification.log").read_text()
                )
            except ValueError:
                value["status"] = "FAIL"
        if name == "acceptance":
            assertions = args.output / "acceptance/assertions.json"
            if assertions.exists():
                checks = json.loads(assertions.read_text())
                value.update(
                    assertions=len(checks), failed=sum(c["status"] != "PASS" for c in checks)
                )
                if value["failed"]:
                    value["status"] = "FAIL"
        results[name] = value
    summary = {
        "status": "PASS"
        if all(value["status"] == "PASS" for value in results.values())
        else "FAIL",
        "group": "documents",
        "suites": results,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "scope": "Isolated PostgreSQL, synthetic loopback resources, all nine formats, offline Replay; live sites excluded",
        "engine_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path("src/seal").rglob("*"))
            if path.is_file() and path.suffix in (".py", ".sql")
        },
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    public = Path("artifacts/ci-public")
    public.mkdir(parents=True, exist_ok=True)
    (public / "documents.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    return int(summary["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
