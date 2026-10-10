"""Publish bounded archive acceptance metadata without raw responses or field values.

Re-run this command against private immutable Harness outputs to reproduce the report.
File hashes bind the public report to local evidence; they do not replace raw-field QA.
"""

import argparse
import hashlib
import json
from pathlib import Path


def report(directory):
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    for name, expected in manifest["files"].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("invalid_artifact_path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("artifact_hash_mismatch")
    assertions = json.loads((directory / "assertions.json").read_bytes())
    checks = [{"name": row["name"], "status": row["status"]} for row in assertions]
    return {
        "directory": str(directory),
        "command": manifest["command"],
        "git_head": manifest.get("git_head"),
        "engine_hashes": {k: v for k, v in manifest["code"].items() if k.startswith("src/")},
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "environment": manifest.get("environment", {"python": manifest["python"]}),
        "assertions": checks,
        "passed": sum(row["status"] == "PASS" for row in checks),
        "failed": sum(row["status"] != "PASS" for row in checks),
        "samples": [
            {
                key: row[key]
                for key in (
                    "research_id",
                    "source_id",
                    "binding_id",
                    "recipe_version",
                    "runs",
                    "status",
                    "runtime_status",
                    "record_count",
                )
                if key in row
            }
            for row in manifest.get("real_source_acceptance", {}).get("samples", [])
        ],
        "evidence_hashes": manifest["files"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    groups = [report(path) for path in args.directory]
    result = {
        "status": "FAIL" if any(group["failed"] for group in groups) else "PASS",
        "scope": "Runtime raw archive; dd-102 bounded iframe; public probe privacy",
        "preconditions": "Python 3.12, uv.lock, PostgreSQL 17+ tools; isolated temporary PG; "
        "umask 077; dd-102 public GET; no production DSN, authentication or query submission",
        "privacy": "Raw objects and receipt values stay in ignored private directories. "
        "Only assertion names/statuses, opaque IDs, environment and hashes are published.",
        "quality_status": "not_evaluated",
        "groups": groups,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "output": str(args.output)}))
    return result["status"] != "PASS"


if __name__ == "__main__":
    raise SystemExit(main())
