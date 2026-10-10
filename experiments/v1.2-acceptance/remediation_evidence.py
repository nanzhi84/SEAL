"""Package remediation runs with the existing artifact and raw-field verifiers.

This only assembles frozen evidence; it never creates or edits Run outcomes.
Use a new output directory, and retain every failed attempt separately.
"""

import argparse
import ast
import gzip
import io
import json
import shlex
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

from review_evidence import demand, encoded, load, outcomes, recipe_proof, sha, source_hashes

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PREFIX = "remediation"


def build(args):
    output = args.output.resolve()
    demand(not output.exists(), "use_new_output_directory")
    engine = source_hashes(ROOT)
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.engine_commit + "^{commit}"], text=True
    ).strip()
    frozen_source = subprocess.check_output(["git", "archive", commit, "src"])
    with tarfile.open(fileobj=io.BytesIO(frozen_source)) as archive:
        committed = {
            member.name: sha(archive.extractfile(member).read())
            for member in archive.getmembers()
            if member.isfile()
        }
    demand(engine == committed, "engine_commit_does_not_match_tested_source")
    groups, payload, current, diagnoses, exceptions = [], {}, {}, {}, []
    from seal.core import contains_sensitive_body

    synthetic = {}
    for path in args.synthetic_fixture:
        path = path.resolve()
        demand(path.is_relative_to(ROOT / "artifacts/acceptance"), "invalid_fixture_path")
        tree = ast.parse(path.read_bytes())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SAFE_BODY" for target in node.targets
            ):
                body = ast.literal_eval(node.value)
                demand(type(body) is bytes, "synthetic_fixture_not_bytes")
                synthetic[sha(body)] = {
                    "fixture": str(path.relative_to(ROOT)),
                    "fixture_sha256": sha(path.read_bytes()),
                    "scope": "Exact authored loopback SAFE_BODY from abandoned getter experiment",
                }

    def add(path, *, synthetic_group=False):
        demand(path.is_file() and not path.is_symlink(), "invalid_evidence_file")
        name = str(path.relative_to(ROOT))
        body = path.read_bytes()
        # Rejected responses never enter the archive. Fail closed if a stored
        # raw object nevertheless contains credentials or a session marker.
        if "/archive/objects/" in name:
            if contains_sensitive_body(body):
                digest = sha(body)
                demand(synthetic_group and digest in synthetic, "sensitive_raw_object:" + name)
                exceptions.append({"path": name, "sha256": digest, **synthetic[digest]})
        payload[name] = body

    for spec in args.group:
        category, name = spec.split(":", 1)
        demand(
            category in {"pass", "mixed_sources", "failure", "fixture_failure"},
            "invalid_group_category",
        )
        directory = Path(name).resolve()
        demand(directory.is_relative_to(ROOT / "artifacts/acceptance"), "invalid_group_path")
        relative = str(directory.relative_to(ROOT))
        group = outcomes(
            ROOT,
            relative,
            {
                "pass": "positive_current_engine",
                "mixed_sources": "mixed_current_engine_sources",
                "failure": "behavior_red",
                "fixture_failure": "invalid_fixture_failure",
            }[category],
            None,
            engine,
        )
        manifest = load(directory / "manifest.json")
        real = manifest.get("real_source_acceptance", {})
        if category == "mixed_sources":
            demand(real.get("status") == "FAIL", "mixed_suite_must_keep_failure")
            recorded = {k: v for k, v in manifest["code"].items() if k.startswith("src/")}
            demand(recorded == engine, "mixed_engine_drift")
            failed = [row for row in real["samples"] if row["status"] == "FAIL"]
            demand(failed, "mixed_source_failure_missing")
            allowed = {"selected_sample_set_processed", "suite_completed"}
            demand(
                all(
                    name in allowed
                    or any(name.startswith(row["source_id"] + "_") for row in failed)
                    for name in group["failed_assertions"]
                ),
                "successful_source_has_failed_assertions",
            )
            group["current_engine"] = True
            group["scope"] = "Suite FAIL preserved; only individually PASS sources accepted"
        if category in {"pass", "mixed_sources"} and (directory / "archive/packages").is_dir():
            group["recipes"] = recipe_proof(directory, engine)
        group["recorded_git_head"] = manifest.get("git_head")
        for sample in real.get("samples", []):
            if category in {"pass", "mixed_sources"} and sample["status"] == "PASS":
                demand(sample["research_id"] not in current, "duplicate_current_source")
                current[sample["research_id"]] = {**sample, "evidence_group": relative}
            elif category in {"pass", "mixed_sources"}:
                path = directory / (sample["source_id"] + "-first.json")
                value = load(path)
                inspection = value.get("inspect", {})
                diagnoses[sample["research_id"]] = {
                    "sample_status": sample["status"],
                    "run_id": value["receipt"].get("run_id"),
                    "runtime_status": value["receipt"].get("status"),
                    "errors": inspection.get("errors", []),
                    "discovery": inspection.get("discovery", {}),
                    "evidence_file": str(path.relative_to(ROOT)),
                }
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                add(path, synthetic_group=real.get("status") not in {"PASS", "FAIL"})
        groups.append(group)

    historical = load(args.history)
    probes = load(args.probes)
    observed = {row["research_id"]: row for row in probes["sources"]}
    rows = []
    for old in historical["outcomes"]:
        rid = old["research_id"]
        row = {**old, "historical_status": old["status"], "current_engine": False}
        if rid in observed:
            row["historical_reason"] = {
                key: old.get(key) for key in ("reason_category", "reason_code", "reason")
            }
            row["current_public_probe"] = observed[rid]
            if old["status"] != "adapted":
                row.update(
                    {
                        key: observed[rid].get(key)
                        for key in ("reason_category", "reason_code", "reason")
                    }
                )
        if rid in current:
            sample = current[rid]
            # Never leave old Run-specific acceptance/recheck fields beside
            # current results. Historical evidence remains complete and named.
            probe = row.get("current_public_probe")
            row = {key: old[key] for key in ("research_id", "class", "name", "entry_url")}
            row.update(historical_evidence=old, historical_status=old["status"])
            if probe:
                row["current_public_probe"] = probe
            row.update(sample)
            row.update(
                current_engine=True,
                status="adapted" if sample["runtime_status"] == "complete" else "partial",
                verification_scope="current_engine_complete_e2e"
                if (sample["runtime_status"] == "complete")
                else "current_engine_partial_boundary_e2e_recheck_rejected",
                reason_code="current_e2e_complete"
                if sample["runtime_status"] == "complete"
                else ",".join(sample["errors"]),
                reason="当前引擎已完成限定样本四轮及独立原文验证"
                if sample["runtime_status"] == "complete"
                else "当前引擎确认正文能力边界，保留原文与 partial",
                recheck="PASS"
                if sample["runtime_status"] == "complete"
                else "REJECTED: no_documents_to_recheck; partial not promoted",
                configuration_evidence=sample["evidence_group"]
                + "/"
                + sample["source_id"]
                + "-configuration.json",
            )
        else:
            row["verification_scope"] = (
                "historical_success_not_revalidated"
                if (old["status"] == "adapted")
                else "current_public_probe_only"
            )
            if rid in diagnoses:
                row["current_engine_diagnostic"] = diagnoses[rid]
                row["verification_scope"] = "current_engine_failed_or_refused_run"
                if rid == "dd-357":
                    row.update(
                        reason_category="来源访问限制 / 安全范围拒绝",
                        reason_code="encoded_seed_redirect_out_of_scope",
                        reason="冻结入口含 %21，与公开探测的 ! 路径不同；本轮重定向到 LicenseRedirect 被边界拒绝，未跟随登录且未复试",
                    )
        rows.append(row)
    disposition = {
        "artifact_version": 1,
        "quality_status": "not_evaluated",
        "synthetic_evidence_exceptions": exceptions,
        "scope": "Bounded sample acceptance; current and historical engine evidence are separate",
        "summary": {
            "total": len(rows),
            "combined_last_known_statuses": dict(Counter(row["status"] for row in rows)),
            "current_engine_statuses": dict(
                Counter(row["status"] for row in rows if row["current_engine"])
            ),
            "historical_adapted_not_revalidated": sum(
                row["status"] == "adapted" and not row["current_engine"] for row in rows
            ),
            "current_engine_diagnostic_sources": sorted(diagnoses),
        },
        "outcomes": rows,
    }
    add(args.probes.resolve())
    for path in args.proof:
        path = path.resolve()
        demand(path.is_relative_to(ROOT / "artifacts/acceptance"), "invalid_proof_path")
        add(path)
    for directory in args.preserve:
        directory = directory.resolve()
        demand(directory.is_relative_to(ROOT / "artifacts/acceptance"), "invalid_preserved_path")
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                add(path)
    for directory in args.baseline:
        directory = directory.resolve()
        demand(directory.is_relative_to(ROOT / "artifacts/acceptance"), "invalid_baseline_path")
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                add(path)
    for folder in (
        "src",
        "recipes",
        "experiments/v1.2-acceptance/remediation_samples",
        "experiments/v1.2-acceptance/delivery_samples",
    ):
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                add(path)
    for folder in ("experiments/v1.1-runtime-acceptance", "experiments/v1.2-acceptance"):
        for path in sorted((ROOT / folder).rglob("*")):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix in {".py", ".sh", ".sql"}
            ):
                add(path)
    add(ROOT / "experiments/source-accessibility/probe.py")
    for name in ("uv.lock", "pyproject.toml"):
        add(ROOT / name)
    payload["dispositions.json"] = encoded(disposition)
    metadata = {
        "artifact_version": 1,
        "engine_commit": commit,
        "engine": engine,
        "groups": groups,
        "source_summary": disposition["summary"],
        "quality_status": "not_evaluated",
        "synthetic_evidence_exceptions": exceptions,
        "files": {name: sha(body) for name, body in sorted(payload.items())},
        "historical_evidence": str(args.history) + "; unchanged",
        "public_probe_scope": "No positive adaptation inference from HTTP status or probes",
        "preserved_development_evidence": [
            {
                "path": str(path.resolve().relative_to(ROOT)),
                "scope": "Preserved RED or abandoned experiment; never final acceptance",
            }
            for path in args.preserve
        ],
        "historical_compatibility_inputs": [
            str(path.resolve().relative_to(ROOT)) for path in args.baseline
        ],
        "additional_offline_proofs": [str(path.resolve().relative_to(ROOT)) for path in args.proof],
        "command": shlex.join(["uv", "run", "--frozen", "python", *sys.argv]),
    }
    payload["package-manifest.json"] = encoded(metadata)
    output.mkdir(parents=True)
    bundle = output / "remediation-evidence.tar.gz"
    with (
        bundle.open("wb") as stream,
        gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for name, body in sorted(payload.items()):
            info = tarfile.TarInfo(PREFIX + "/" + name)
            info.size, info.mode, info.mtime = len(body), 0o644, 0
            archive.addfile(info, io.BytesIO(body))
    metadata["public_archive"] = {
        "path": bundle.name,
        "sha256": sha(bundle.read_bytes()),
        "bytes": bundle.stat().st_size,
    }
    (output / "remediation-evidence-index.json").write_bytes(encoded(metadata))
    (output / "dispositions.json").write_bytes(encoded(disposition))
    return metadata["source_summary"]


def verify(index_path):
    index = load(index_path)
    bundle = index_path.parent / index["public_archive"]["path"]
    demand(sha(bundle.read_bytes()) == index["public_archive"]["sha256"], "package_sha")
    commands = []
    with tempfile.TemporaryDirectory(prefix="seal-remediation-verify-") as temporary:
        destination = Path(temporary)
        with tarfile.open(bundle, "r:gz") as archive:
            archive.extractall(destination, filter="data")
        root = destination / PREFIX
        for name, expected in index["files"].items():
            path = (root / name).resolve()
            demand(path.is_relative_to(root.resolve()), "package_file_path_escape")
            demand(sha(path.read_bytes()) == expected, "package_file_sha:" + name)
        for group in index["groups"]:
            directory = root / group["path"]
            command = [sys.executable, str(HERE / "verify_artifacts.py"), str(directory)]
            recorded = (
                group["category"] == "mixed_current_engine_sources" or not group["current_engine"]
            )
            if recorded:
                command.append("--recorded-outcomes")
            result = subprocess.run(command, capture_output=True, text=True)
            demand(result.returncode == 0, "artifact_verification:" + group["path"])
            commands.append(json.loads(result.stdout))
            manifest = load(directory / "manifest.json")
            if group["current_engine"] and manifest.get("real_source_acceptance", {}).get(
                "status"
            ) in {"PASS", "FAIL"}:
                command = [sys.executable, str(HERE / "live_verify.py"), str(directory)]
                if recorded:
                    command.append("--recorded-outcomes")
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                )
                demand(result.returncode == 0, "independent_raw_fields:" + group["path"])
                commands.append(json.loads(result.stdout))
    return {"status": "PASS", "verified_files": len(index["files"]), "checks": commands}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--probes", type=Path)
    parser.add_argument("--engine-commit")
    parser.add_argument(
        "--history", type=Path, default=HERE / "results/expanded-final/dispositions.json"
    )
    parser.add_argument("--preserve", type=Path, action="append", default=[])
    parser.add_argument("--baseline", type=Path, action="append", default=[])
    parser.add_argument("--proof", type=Path, action="append", default=[])
    parser.add_argument("--synthetic-fixture", type=Path, action="append", default=[])
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.verify) if args.verify else build(args)
    if args.report:
        args.report.write_bytes(encoded(result))
    print(json.dumps(result, ensure_ascii=False))
