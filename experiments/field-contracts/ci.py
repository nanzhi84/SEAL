"""Run field contracts and independent raw-byte oracle; publish metadata only."""

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
    run = subprocess.run(
        [sys.executable, "experiments/field-contracts/acceptance.py", "--output", str(args.output)],
        check=False,
    )
    verification = subprocess.run(
        [sys.executable, "experiments/field-contracts/verify.py", str(args.output)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        oracle = json.loads(verification.stdout)
    except ValueError:
        oracle = {"status": "FAIL", "reason": "oracle_output_missing"}
    summary = {
        "status": "PASS" if run.returncode == verification.returncode == 0 else "FAIL",
        "group": "fields",
        "acceptance_exit": run.returncode,
        "verification_exit": verification.returncode,
        "independent_oracle": oracle,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "scope": "Isolated PostgreSQL, two synthetic sources, public Vietnamese archived fixture; CI excludes live sites",
        "engine_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path("src/seal").rglob("*"))
            if p.is_file() and p.suffix in (".py", ".sql")
        },
    }
    public = Path("artifacts/ci-public")
    public.mkdir(parents=True, exist_ok=True)
    (public / "fields.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    return int(summary["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
