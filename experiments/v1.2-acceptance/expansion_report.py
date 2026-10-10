"""Assemble frozen real acceptance, with complete ABC disposition coverage.

Fail before delivery on missing/duplicate IDs, unverified files, failed suites,
changed Runtime, mismatched successful contracts or a ready Source without a Run.
Diagnostics describe observed blockers; they cannot promote a Source to adapted.
"""

import argparse
import hashlib
import json
import shlex
import shutil
import sys
from collections import Counter
from pathlib import Path

from live_verify import verify_export
from public_evidence import SESSION, package

ROOT = Path(__file__).resolve().parents[2]


def sha(body):
    return hashlib.sha256(body).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read(path):
    return json.loads(path.read_text())


def acceptance_metrics(directory):
    samples = read(directory / "manifest.json")["real_source_acceptance"]["samples"]
    projections = 0
    for path in directory.glob("*.json"):
        data = read(path)
        if isinstance(data, dict) and "export" in data:
            projections += len(data["export"]["records"])
    return {
        "accepted_samples": len(samples),
        "final_acceptance_run_count": sum(len(sample["runs"]) for sample in samples),
        "first_round_record_count": sum(sample["record_count"] for sample in samples),
        "record_projection_count": projections,
        "historical_failed_runs_included": False,
    }


def merge_file(source, target, expected=None):
    body = source.read_bytes()
    if expected and sha(body) != expected:
        raise ValueError(f"artifact_changed:{source}")
    if target.exists():
        if target.read_bytes() != body:
            raise ValueError(f"artifact_collision:{target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def assemble(evidence, diagnostics, failures, destination):
    if destination.exists():
        raise ValueError("Use a new evidence directory")
    destination.mkdir(parents=True)
    inventory = read(ROOT / "experiments/source-accessibility/results/classification/entries.json")
    selected = {row["id"]: row for row in inventory if row["class"] in "ABC"}
    runtime = {
        str(path.relative_to(ROOT)): sha(path.read_bytes())
        for path in (ROOT / "src").rglob("*")
        if path.suffix in (".py", ".sql") and "__pycache__" not in path.parts
    }
    assertions, samples, runs, environments = [], [], [], []
    source_ids, successful = set(), {}
    for group, directory in evidence:
        manifest = read(directory / "manifest.json")
        if manifest["real_source_acceptance"]["status"] != "PASS":
            raise ValueError(f"failed_acceptance:{directory}")
        if any(manifest["code"].get(name) != expected for name, expected in runtime.items()):
            raise ValueError(f"runtime_changed:{directory}")
        for name, expected in manifest.get("input_samples", {}).items():
            contract = Path(name)
            if not contract.is_absolute():
                contract = ROOT / contract
            if sha(contract.read_bytes()) != expected:
                raise ValueError(f"sample_contract_changed:{contract}")
        checks = read(directory / "assertions.json")
        if any(c["status"] != "PASS" or c["actual"] != c["expected"] for c in checks):
            raise ValueError(f"untruthful_acceptance:{directory}")
        assertions.extend({**check, "name": group + ":" + check["name"]} for check in checks)
        for name, expected in manifest["files"].items():
            source = (directory / name).resolve()
            if not source.is_relative_to(directory.resolve()):
                raise ValueError("artifact_path_escape")
            if name.startswith("archive/"):
                target = destination / name
            else:
                target = destination / "runs" / group / name
            merge_file(source, target, expected)
            if source.suffix == ".json" and "/" not in name:
                data = read(source)
                if isinstance(data, dict) and "export" in data:
                    errors = verify_export(directory / "archive", data["export"])
                    if errors:
                        raise ValueError(f"field_verification_failed:{name}:{errors[:1]}")
                    merge_file(source, destination / (group + "-" + name), expected)
        merge_file(directory / "manifest.json", destination / "runs" / group / "manifest.json")
        for sample in manifest["real_source_acceptance"]["samples"]:
            rid, sid = sample["research_id"], sample["source_id"]
            if rid in successful or sid in source_ids or rid not in selected:
                raise ValueError(f"duplicate_or_unselected_source:{rid}")
            if sample["status"] != "PASS" or sample["class"] != selected[rid]["class"]:
                raise ValueError(f"sample_not_passed:{rid}")
            configuration = read(directory / (sid + "-configuration.json"))
            successful[rid] = {**sample, "recipe": configuration["recipe"], "group": group}
            source_ids.add(sid)
            samples.append(sample)
        environments.append(manifest["environment"])
        runs.append(
            {
                "group": group,
                "directory": str(directory),
                "manifest_sha256": sha((directory / "manifest.json").read_bytes()),
                "command": manifest["command"],
            }
        )
    dispositions = {}
    for path in diagnostics:
        document = read(path)
        rows = document["outcomes"]
        ids = [row["research_id"] for row in rows]
        if sorted(ids) != sorted(document["assigned_ids"]) or len(set(ids)) != len(ids):
            raise ValueError(f"incomplete_diagnostics:{path}")
        for row in rows:
            rid = row["research_id"]
            if rid in dispositions or rid not in selected or row["class"] != selected[rid]["class"]:
                raise ValueError(f"duplicate_diagnostic:{rid}")
            if not row.get("reason") or not row.get("reason_code") or not row.get("evidence"):
                raise ValueError(f"missing_blocker_context:{rid}")
            if row["status"] == "ready" and rid not in successful:
                raise ValueError(f"ready_without_accepted_run:{rid}")
            dispositions[rid] = row
        merge_file(path, destination / "diagnostics" / path.name)
    failed_runs = []
    for group, directory in failures:
        manifest = read(directory / "manifest.json")
        checks = read(directory / "assertions.json")
        if any(
            check["status"] != ("PASS" if check["actual"] == check["expected"] else "FAIL")
            for check in checks
        ):
            raise ValueError(f"untruthful_failed_outcomes:{directory}")
        for name, expected in manifest["files"].items():
            source = (directory / name).resolve()
            if not source.is_relative_to(directory.resolve()):
                raise ValueError("failed_artifact_path_escape")
            merge_file(source, destination / "failures" / group / name, expected)
        merge_file(directory / "manifest.json", destination / "failures" / group / "manifest.json")
        failed_runs.append(
            {
                "group": group,
                "directory": str(directory),
                "recorded_manifest": f"failures/{group}/manifest.json",
                "manifest_sha256": sha((directory / "manifest.json").read_bytes()),
                "command": manifest["command"],
                "passed_assertions": sum(check["status"] == "PASS" for check in checks),
                "failed_assertions": [
                    check["name"] for check in checks if check["status"] == "FAIL"
                ],
                "included_in_final_pass_totals": False,
            }
        )
    report = []
    for rid, original in sorted(selected.items()):
        observed = dispositions.get(rid)
        accepted = successful.get(rid)
        if not accepted and not observed:
            raise ValueError(f"unprocessed_source:{rid}")
        row = {
            "research_id": rid,
            "class": original["class"],
            "name": original["name"],
            "entry_url": original["entry_url"],
        }
        if observed:
            row.update({k: v for k, v in observed.items() if k not in row})
        if accepted:
            row.update(accepted)
            row["status"] = "adapted" if accepted["runtime_status"] == "complete" else "partial"
            if not observed:
                row.update(
                    reason_code="accepted_existing_sample"
                    if row["status"] == "adapted"
                    else accepted["errors"][0],
                    reason="既有真实限定样本通过，保留原验收日期与版本"
                    if row["status"] == "adapted"
                    else "原始附件已归档；当前文本能力不能解析其正文",
                    evidence=[{"acceptance_group": accepted["group"], "run_ids": accepted["runs"]}],
                    next_step="扩大范围或恢复附件能力前重新验收",
                )
        else:
            if observed["status"] not in ("blocked", "failed"):
                raise ValueError(f"unfinished_source:{rid}")
            row["status"] = "blocked" if observed["status"] == "blocked" else "acceptance_failed"
        report.append(row)
    counts = Counter(row["status"] for row in report)
    by_class = {
        category: dict(Counter(row["status"] for row in report if row["class"] == category))
        for category in "ABC"
    }
    disposition = {
        "artifact_version": 1,
        "scope": "All 111 original ABC research IDs; bounded public sample adaptation, not whole-site or subject verification",
        "input_classification_sha256": sha(
            (
                ROOT / "experiments/source-accessibility/results/classification/entries.json"
            ).read_bytes()
        ),
        "summary": {"total": len(report), "statuses": dict(counts), "by_class": by_class},
        "outcomes": report,
        "acceptance_runs": runs,
        "historical_failed_runs": failed_runs,
        "quality_status": "not_evaluated",
    }
    if SESSION.search(json.dumps(disposition, ensure_ascii=False).encode()):
        raise ValueError("Diagnostics must not contain session credentials")
    write(destination / "dispositions.json", disposition)
    write(destination / "assertions.json", assertions)
    files = {
        str(path.relative_to(destination)): sha(path.read_bytes())
        for path in destination.rglob("*")
        if path.is_file()
    }
    manifest = {
        "artifact_version": 1,
        "stage": "expanded-real-sources",
        "scope": disposition["scope"],
        "git_head": read(evidence[0][1] / "manifest.json")["git_head"],
        "code": runtime,
        "command": "Run each frozen acceptance_runs command in dispositions.json with a new output directory",
        "environment": {
            "groups": environments,
            "isolation": "Independent disposable loopback PostgreSQL; direct public GET; no authentication or cookies",
        },
        "files": files,
        "real_source_acceptance": {"status": "PASS", "samples": samples},
        "unverified": [
            "Whole-site completeness",
            "Formal business Evaluation",
            "Production scale",
            "Unsupported attachment text extraction",
        ],
    }
    write(destination / "manifest.json", manifest)
    return disposition


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence", action="append", required=True, help="GROUP=ACCEPTANCE_DIRECTORY"
    )
    parser.add_argument("--diagnostics", type=Path, action="append", required=True)
    parser.add_argument("--failure-evidence", action="append", default=[])
    parser.add_argument("--local-output", type=Path, required=True)
    parser.add_argument("--public-output", type=Path, required=True)
    args = parser.parse_args()
    pairs = [
        (group, Path(directory).resolve())
        for group, directory in (arg.split("=", 1) for arg in args.evidence)
    ]
    if len({group for group, _ in pairs}) != len(pairs) or any(
        not group.isidentifier() for group, _ in pairs
    ):
        raise ValueError("Evidence group names must be distinct identifiers")
    failure_pairs = [
        (group, Path(directory).resolve())
        for group, directory in (arg.split("=", 1) for arg in args.failure_evidence)
    ]
    if len({group for group, _ in failure_pairs}) != len(failure_pairs) or any(
        not group.isidentifier() for group, _ in failure_pairs
    ):
        raise ValueError("Failure group names must be distinct identifiers")
    result = assemble(pairs, args.diagnostics, failure_pairs, args.local_output.resolve())
    package(args.local_output.resolve(), args.public_output.resolve())
    index_path = args.public_output / "real-evidence-index.json"
    index = read(index_path)
    index.update(
        expanded_coverage=result["summary"],
        dispositions="real/dispositions.json inside real-evidence.tar.gz",
        acceptance_runs=result["acceptance_runs"],
        acceptance_metrics=acceptance_metrics(args.local_output.resolve()),
        assembly_command=shlex.join(["uv", "run", "--frozen", "python", *sys.argv]),
    )
    write(index_path, index)
    write(args.public_output / "dispositions.json", result)
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
