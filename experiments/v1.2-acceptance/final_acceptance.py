"""Recompute the PR delivery gate from private, immutable acceptance artifacts.

No collection or product feature lives here. Existing independent verifiers
check recorded outcomes and raw business fields; publication contains hashes
and assertion names/statuses, never original response bytes or field values.
"""

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

from archive_report import report

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
REQUIRED = {
    "joint",
    "public-probe",
    "m3",
    "smoke",
    "pagination",
    "parents",
    "recheck",
    "real",
    "blocked",
    "blank-query",
    "dd009",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise ValueError("independent_verifier_failed")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", action="append", required=True, help="name=private-directory")
    parser.add_argument("--t7s", type=Path, required=True)
    parser.add_argument("--pagination-proof", type=Path, required=True)
    parser.add_argument("--failed-attempt", type=Path, required=True)
    parser.add_argument("--engine-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    directories = dict(value.split("=", 1) for value in args.suite)
    if set(directories) != REQUIRED or len(args.suite) != len(REQUIRED):
        raise ValueError("required_suite_set_mismatch")
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.engine_commit + "^{commit}"], text=True
    ).strip()
    groups = {}
    for name, value in directories.items():
        directory = Path(value)
        metadata = report(directory)
        manifest = json.loads((directory / "manifest.json").read_bytes())
        # Match all product/recipe inputs to the exact accepted commit, not only HEAD labels.
        for path, expected in manifest["code"].items():
            if path.startswith(("src/", "recipes/")):
                body = subprocess.check_output(["git", "show", commit + ":" + path])
                if hashlib.sha256(body).hexdigest() != expected:
                    raise ValueError("engine_or_recipe_drift")
        real = manifest.get("real_source_acceptance", {}).get("status")
        verifier = "live_verify.py" if real in {"PASS", "FAIL"} else "verify_artifacts.py"
        outcome = verify([sys.executable, str(HERE / verifier), str(directory)])
        groups[name] = {
            "status": "FAIL" if metadata["failed"] else "PASS",
            **metadata,
            "independent_verification": outcome,
        }
    real_directory = Path(directories["real"])
    from delivery_verify import verify as verify_delivery

    gate = verify_delivery(real_directory)
    frozen_gate = json.loads(args.t7s.read_bytes())
    if gate != frozen_gate:
        raise ValueError("t7s_proof_drift")
    pagination = json.loads(args.pagination_proof.read_bytes())
    from full_pagination_verify import verify as verify_pagination

    manifest = json.loads((real_directory / "manifest.json").read_bytes())
    if len(manifest["input_samples"]) != 1:
        raise ValueError("ambiguous_frozen_samples")
    sample_path = Path(next(iter(manifest["input_samples"])))
    if verify_pagination(real_directory.resolve(), sample_path, "dd-250") != pagination:
        raise ValueError("pagination_proof_drift")
    failed = report(args.failed_attempt)
    verify(
        [
            sys.executable,
            str(HERE / "verify_artifacts.py"),
            str(args.failed_attempt),
            "--recorded-outcomes",
        ]
    )
    passed = all(row["status"] == "PASS" for row in groups.values())
    passed = passed and gate["status"] == pagination["status"] == "PASS"
    result = {
        "status": "PASS" if passed else "FAIL",
        "engine_commit": commit,
        "work_packages": {
            **{f"T{i}": "PASS" if passed else "FAIL" for i in range(7)},
            "T7S": gate["status"],
            "T8": "PASS" if passed else "FAIL",
        },
        "groups": groups,
        "T7S": {
            "status": gate["status"],
            "counts": gate["counts"],
            "assertions": [{"name": x["name"], "status": x["status"]} for x in gate["assertions"]],
            "mechanisms": gate["mechanisms"],
            "private_proof": str(args.t7s),
            "sha256": sha(args.t7s),
        },
        "full_pagination": {
            "status": pagination["status"],
            "private_proof": str(args.pagination_proof),
            "sha256": sha(args.pagination_proof),
            "checks": len(pagination["checks"]),
        },
        "resolved_failure": {
            "initial_status": "FAIL",
            "reason": "Legacy M3 required sensitive body refusal",
            "fix": "Remove obsolete body refusal assertions and unused fixture; retain URL controls",
            "manifest_sha256": failed["manifest_sha256"],
            "private_directory": str(args.failed_attempt),
            "failed_assertions": [x["name"] for x in failed["assertions"] if x["status"] != "PASS"],
            "rerun_status": groups["m3"]["status"],
        },
        "source_status": {
            **{
                row["research_id"]: row["status"]
                for name in ("real", "dd009")
                for row in groups[name]["samples"]
            },
            "dd-357": "BLOCKED: public redirect leaves approved scope; refusal diagnostic PASS",
        },
        "privacy": "Private raw archives are not published; only metadata, hashes and checks",
        "quality_status": "not_evaluated",
        "unverified": [
            "111-source full rerun",
            "Evaluation/business quality approval",
            "Power loss durability",
        ],
        "command": "uv run --frozen python experiments/v1.2-acceptance/final_acceptance.py "
        + shlex.join(sys.argv[1:]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "work_packages": result["work_packages"]}))
    return not passed


if __name__ == "__main__":
    raise SystemExit(main())
