"""Deterministic review evidence package, truthful outcomes and offline SHA checks."""

import argparse
import base64
import gzip
import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

OUTPUT = Path("experiments/v1.2-acceptance/results/review-fixes-final")
INDEX = "review-evidence-index.json"
PREFIX = "review"
GROUPS = [
    ("pointer", "v1.2-json-pointer-final", "positive_current_engine", 36043),
    ("cache_boundaries", "v1.2-json-pointer-boundaries-complete", "positive_current_engine", 91),
    ("recheck", "v1.2-recheck-final", "positive_current_engine", 115),
    ("pagination", "v1.2-pagination-final", "positive_current_engine", 154),
    ("amac_full", "v1.2-amac-full-final", "positive_current_engine", 77),
    ("joint", "v1.2-review-joint-final", "positive_current_engine", 419),
    ("proxy", "v1.2-review-proxy-final", "positive_current_engine", 13),
    ("migration", "v1.2-review-m3-final-v2", "positive_current_engine", 136),
    ("smoke", "v1.2-review-smoke-final-v2", "positive_current_engine", 1200),
    ("real_abc", "v1.2-review-real-abc-final", "positive_current_engine", 232),
    ("pointer_baseline", "v1.2-json-pointer-baseline", "performance_red", 24024),
    ("proxy_red", "v1.2-environment-proxy-red", "behavior_red", None),
    ("identity_red", "v1.2-recheck-identity-red3", "behavior_red", None),
    ("due_batch_red", "v1.2-recheck-batch-red2", "behavior_red", None),
    ("parent_dedup_red", "v1.2-recheck-failure-red2", "behavior_red", None),
    ("pagination_red", "v1.2-pagination-red", "behavior_red", None),
    ("pagination_entry_red", "v1.2-pagination-wrong-entry-target-red", "behavior_red", None),
    (
        "cache_fixture_failure",
        "v1.2-json-pointer-boundaries-final",
        "invalid_fixture_failure",
        None,
    ),
    ("migration_oracle_failure", "v1.2-review-m3-final", "invalid_oracle_failure", None),
    ("smoke_fixture_failure", "v1.2-review-smoke-final", "invalid_fixture_failure", None),
    (
        "pagination_fixture_failure",
        "v1.2-pagination-wrong-entry-red",
        "invalid_fixture_failure",
        None,
    ),
    ("recheck_import_failure", "v1.2-recheck-identity-red", "invalid_infrastructure_failure", None),
    ("recheck_drift_failure", "v1.2-recheck-identity-red2", "invalid_infrastructure_failure", None),
]
REASONS = {
    "pointer_baseline": "Business values pass; measured CPU exceeds the frozen improvement targets.",
    "cache_fixture_failure": "Test Source used a non-path prefix; Source correctly rejected its two entries.",
    "migration_oracle_failure": "Oracle compared whole row shapes; new nullable recheck_plan changed shape, no pre-upgrade value changed.",
    "smoke_fixture_failure": "C01 restored pre-Records SQL and skipped public db migrate; 1188 assertions pass but C01 fails.",
    "pagination_fixture_failure": "Wrong-entry fixture failed before the target behavior with missing run_id.",
    "recheck_import_failure": "Concurrent validation-module migration caused crawl_process_failed; invalid domain RED.",
    "recheck_drift_failure": "Concurrent engine changes caused environment_drift; invalid domain RED.",
}
SESSION = re.compile(rb"jsessionid[=:\s\"']+[A-Za-z0-9._-]{16,}", re.I)
SECRET = re.compile(
    rb"(?:access[_-]?token|api[_-]?key|password|authorization|secret)\s*[\"']?\s*[:=]\s*[\"']?([^\s<\"',}]+)",
    re.I,
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(path.read_text())


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def source_hashes(root):
    return {
        str(path.relative_to(root)): sha(path.read_bytes())
        for path in sorted((root / "src").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def demand(condition, code):
    if not condition:
        raise ValueError(code)


def safe(path, body):
    demand(not SESSION.search(body), "session_bearing_file:" + path)
    if "/archive/objects/" in path:
        for found in SECRET.finditer(body):
            value = found[1].lower()
            demand(
                value in {b"null", b"none", b"redacted"} or b"synthetic" in value,
                "sensitive_raw_file:" + path,
            )


def outcomes(root, relative, category, expected_count, engine):
    directory = root / relative
    manifest = load(directory / "manifest.json")
    for name, expected in manifest["files"].items():
        path = directory / name
        demand(path.resolve().is_relative_to(directory.resolve()), "artifact_path_escape")
        demand(path.is_file() and sha(path.read_bytes()) == expected, "artifact_hash:" + str(path))
    assertions = load(directory / "assertions.json")
    for row in assertions:
        demand(
            row["status"] == ("PASS" if row["actual"] == row["expected"] else "FAIL"),
            "untruthful_assertion:" + row["name"],
        )
    failures = [row["name"] for row in assertions if row["status"] != "PASS"]
    summary = load(directory / "summary.json") if (directory / "summary.json").exists() else {}
    cases = summary.get("cases", [])
    failed_cases = [row["id"] for row in cases if row["status"] != "PASS"]
    current = category == "positive_current_engine"
    if current:
        recorded = {
            name: value for name, value in manifest["code"].items() if name.startswith("src/")
        }
        demand(recorded == engine, "positive_engine_drift:" + relative)
        demand(
            not failures and not failed_cases and not summary.get("error"),
            "positive_business_failure:" + relative,
        )
        demand(
            expected_count is None or len(assertions) == expected_count,
            "assertion_contract:" + relative,
        )
        if "smoke" in directory.name:
            required = (
                {f"G{i:02}" for i in range(1, 9)}
                | {f"R{i:02}" for i in range(1, 13)}
                | {f"N{i:02}" for i in range(1, 12)}
                | {"C01", "X01"}
            )
            demand(
                len(cases) == len(required) and {row["id"] for row in cases} == required,
                "smoke_case_contract",
            )
        real = manifest.get("real_source_acceptance", {})
        if real.get("status") != "NOT_INCLUDED" and real:
            demand(real.get("status") == "PASS", "real_source_suite_failed")
            demand(
                all(row["research_id"] != "dd-102" for row in real.get("samples", [])),
                "private_source_included",
            )
    elif category != "performance_red":
        demand(failures or failed_cases, "failure_evidence_missing:" + relative)
    return {
        "path": relative,
        "category": category,
        "manifest_sha256": sha((directory / "manifest.json").read_bytes()),
        "manifest_files": len(manifest["files"]),
        "current_engine": current,
        "functional_status": "FAIL" if failures or failed_cases else "PASS",
        "live_assertions": {
            "total": len(assertions),
            "passed": len(assertions) - len(failures),
            "failed": len(failures),
        },
        "failed_assertions": failures,
        "cases": cases,
        "failed_cases": failed_cases,
        "command": manifest["command"],
    }


def recipe_proof(directory, engine):
    source_engine = {name.removeprefix("src/seal/"): value for name, value in engine.items()}
    seen, materialized = set(), 0
    for path in sorted(directory.glob("*.json")):
        if path.name == "manifest.json":
            continue
        value = load(path)
        inspection = value.get("inspect", {}) if isinstance(value, dict) else {}
        manifest = inspection.get("manifest", {})
        if manifest.get("run_status") == "complete":
            seen.add(manifest["recipe_version"])
    packages = directory / "archive/packages"
    versions = seen | {
        path.name
        for path in packages.iterdir()
        if path.is_dir() and re.fullmatch(r"[0-9a-f]{64}", path.name)
    }
    historical = []
    for version in sorted(versions):
        raw = directory / "archive/objects" / version[:2] / version[2:]
        demand(raw.is_file() and sha(raw.read_bytes()) == version, "recipe_object_hash")
        bundle = load(raw)
        if bundle["environment"]["engine"] != source_engine:
            demand(version not in seen, "accepted_recipe_engine_drift")
            historical.append(version)
        for name, data in bundle["files"].items():
            material = directory / "archive/packages" / version / name
            demand(material.read_bytes() == base64.b64decode(data), "recipe_materialization_hash")
            materialized += 1
    return {
        "accepted_recipe_versions": sorted(seen),
        "bundled_recipe_versions": sorted(versions),
        "historical_recipe_versions": historical,
        "materialized_files_verified": materialized,
    }


def build(output):
    root = Path.cwd().resolve()
    engine = source_hashes(root)
    payload, groups = {}, []

    def add(path):
        demand(path.is_file() and not path.is_symlink(), "missing_or_symlink_input:" + str(path))
        relative = str(path.relative_to(root))
        body = path.read_bytes()
        safe(relative, body)
        payload[relative] = body

    for identifier, name, category, count in GROUPS:
        relative = "artifacts/acceptance/" + name
        group = outcomes(root, relative, category, count, engine)
        group.update(id=identifier, reason=REASONS.get(identifier))
        if group["current_engine"]:
            group["recipes"] = recipe_proof(root / relative, engine)
        for path in sorted((root / relative).rglob("*")):
            if path.is_file():
                add(path)
        groups.append(group)
    compatibility = Path(
        "artifacts/acceptance/v1.2-review-compatibility-input/regression/original-baseline"
    )
    count = 0
    for path in sorted((root / compatibility).rglob("*")):
        if path.is_file():
            add(path)
            count += 1
    demand(count == 62, "historical_input_file_count")
    groups.append(
        {
            "id": "compatibility_input",
            "path": str(compatibility),
            "category": "historical_input",
            "current_engine": False,
            "files": count,
        }
    )
    for folder in ("src", "experiments/v1.1-runtime-acceptance", "experiments/v1.2-acceptance"):
        for path in sorted((root / folder).rglob("*")):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix in {".py", ".sh", ".sql"}
            ):
                add(path)
    for folder in (
        "recipes/generic",
        "recipes/heterogeneous",
        "recipes/amac_full",
        "recipes/live_a",
        "recipes/live_b",
        "recipes/live_c",
        "experiments/seal-v1.1-runtime-golden-fixtures",
        "experiments/v1.2-acceptance/full_pagination",
    ):
        for path in sorted((root / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                add(path)
    for path in sorted((root / "experiments/v1.2-acceptance/live_samples").glob("*.json")):
        add(path)
    extras = [
        "uv.lock",
        "pyproject.toml",
        ".python-version",
        "experiments/v1.2-acceptance/results/json-pointer-evidence-index.json",
        "experiments/v1.2-acceptance/results/json-pointer-baseline-performance-red.json",
        "experiments/v1.2-acceptance/results/json-pointer-baseline-verification-pre-namespace-fix.json",
        str(OUTPUT / "migration-oracle-diagnosis.json"),
        str(OUTPUT / "pre-package-independent-review.json"),
        "artifacts/acceptance/v1.2-review-refactor/review.json",
        "artifacts/acceptance/v1.2-pagination-verification.json",
        "artifacts/acceptance/v1.2-amac-full-independent-verification.json",
    ]
    for name in extras:
        add(root / name)
    for path in sorted((root / "artifacts/acceptance/v1.2-review-doc-qa").rglob("*")):
        if path.is_file():
            add(path)
    historical = load(
        root / "experiments/v1.2-acceptance/results/expanded-final/real-evidence-index.json"
    )
    old_manifest = root / "artifacts/acceptance/v1.2-expanded-all-final/manifest.json"
    before = load(root / "artifacts/acceptance/v1.2-json-pointer-baseline/summary.json")
    after = load(root / "artifacts/acceptance/v1.2-json-pointer-final/summary.json")
    comparisons = []
    for kind in ("run", "replay"):
        old = next(row for row in before["timings"] if row["args"][0] == kind)
        new = next(row for row in after["timings"] if row["args"][0] == kind)
        demand(
            new["cpu_seconds"]
            <= old["cpu_seconds"] * before["performance_contract"]["cpu_ratio_max"],
            "performance_target_not_met",
        )
        comparisons.append(
            {
                "kind": kind,
                "before_cpu_seconds": old["cpu_seconds"],
                "after_cpu_seconds": new["cpu_seconds"],
                "baseline_performance": "RED",
                "final_performance": "GREEN",
            }
        )
    metadata = {
        "artifact_version": 1,
        "archive_prefix": PREFIX,
        "scope": "Four review fixes: planned Recheck identity/due work, full pagination and native Pointer performance",
        "package_integrity_status": "PASS",
        "positive_acceptance_status": "PASS",
        "contains_preserved_failures": True,
        "groups": groups,
        "engine": engine,
        "performance": comparisons,
        "files": {name: sha(body) for name, body in sorted(payload.items())},
        "disclosure": {
            "withheld_files": [],
            "excluded_live_source_ids": ["dd-102"],
            "full_public_raw_inputs_preserved": True,
        },
        "historical_expansion": {
            "status": "HISTORICAL_ENGINE_ONLY",
            "index": "../expanded-final/real-evidence-index.json",
            "package": "../expanded-final/real-evidence.tar.gz",
            "archive": historical["public_archive"],
            "engine": {
                name: value
                for name, value in load(old_manifest)["code"].items()
                if name.startswith("src/")
            },
            "manifest_sha256": sha(old_manifest.read_bytes()),
            "included_in_this_archive": False,
        },
        "independent_review": {
            "path": "independent-review.json",
            "status_at_packaging": "PENDING_MASTER_PACKAGE",
            "pre_package_report": str(OUTPUT / "pre-package-independent-review.json"),
            "external_report_not_hash_locked": True,
        },
        "commands": {
            "build": "uv run --frozen python experiments/v1.2-acceptance/review_evidence.py",
            "verify": "uv run --frozen python experiments/v1.2-acceptance/review_evidence.py --verify "
            + str(OUTPUT / INDEX)
            + " --current-src",
        },
    }
    payload["review-manifest.json"] = encoded(metadata)
    metadata["files"] = {name: sha(body) for name, body in sorted(payload.items())}
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="seal-review-package-") as temporary:
        candidate = Path(temporary) / "bundle.tar.gz"
        with (
            candidate.open("wb") as stream,
            gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as zipped,
            tarfile.open(fileobj=zipped, mode="w") as archive,
        ):
            for name, body in sorted(payload.items()):
                info = tarfile.TarInfo(PREFIX + "/" + name)
                info.size, info.mode, info.mtime = (
                    len(body),
                    0o755 if name.endswith(".sh") else 0o644,
                    0,
                )
                archive.addfile(info, io.BytesIO(body))
        archive_path = output / "review-evidence.tar.gz"
        archive_sha = sha(candidate.read_bytes())
        if archive_path.exists():
            demand(sha(archive_path.read_bytes()) == archive_sha, "existing_package_differs")
        else:
            shutil.copyfile(candidate, archive_path)
    metadata["public_archive"] = {
        "path": archive_path.name,
        "bytes": archive_path.stat().st_size,
        "sha256": archive_sha,
        "files": len(payload),
        "uncompressed_bytes": sum(map(len, payload.values())),
    }
    data = encoded(metadata)
    if (output / INDEX).exists():
        demand((output / INDEX).read_bytes() == data, "existing_index_differs")
    else:
        (output / INDEX).write_bytes(data)
    return {"index": str(output / INDEX), "index_sha256": sha(data), **metadata["public_archive"]}


def verify(index_path, current_src):
    index = load(index_path)
    bundle = index_path.parent / index["public_archive"]["path"]
    demand(
        bundle.stat().st_size == index["public_archive"]["bytes"]
        and sha(bundle.read_bytes()) == index["public_archive"]["sha256"],
        "package_sha_or_size",
    )
    demand(index["public_archive"]["files"] == len(index["files"]), "file_count")
    with tempfile.TemporaryDirectory(prefix="seal-review-offline-") as temporary:
        root = Path(temporary) / PREFIX
        found = set()
        with tarfile.open(bundle, "r:gz") as archive:
            for member in archive:
                name = member.name.removeprefix(PREFIX + "/")
                demand(
                    member.isfile()
                    and member.name.startswith(PREFIX + "/")
                    and name in index["files"]
                    and name not in found,
                    "unsafe_or_extra_member",
                )
                path = root / name
                demand(path.resolve().is_relative_to(root.resolve()), "package_path_escape")
                data = archive.extractfile(member).read()
                demand(sha(data) == index["files"][name], "package_member_hash:" + name)
                safe(name, data)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                found.add(name)
        demand(found == set(index["files"]), "missing_member")
        demand(source_hashes(root) == index["engine"], "bundled_src_drift")
        if current_src:
            demand(source_hashes(Path.cwd().resolve()) == index["engine"], "current_src_drift")
        totals = {
            "positive_assertions": 0,
            "preserved_failed_assertions": 0,
            "preserved_failed_cases": 0,
        }
        for group in index["groups"]:
            if group["category"] == "historical_input":
                continue
            actual = outcomes(
                root,
                group["path"],
                group["category"],
                group["live_assertions"]["total"],
                index["engine"],
            )
            for name in (
                "manifest_sha256",
                "live_assertions",
                "failed_assertions",
                "failed_cases",
                "functional_status",
            ):
                demand(actual[name] == group[name], "group_projection_drift:" + group["id"])
            if group["current_engine"]:
                demand(
                    recipe_proof(root / group["path"], index["engine"]) == group["recipes"],
                    "recipe_projection_drift",
                )
                totals["positive_assertions"] += group["live_assertions"]["total"]
            totals["preserved_failed_assertions"] += group["live_assertions"]["failed"]
            totals["preserved_failed_cases"] += len(group["failed_cases"])
    return {
        "status": "PASS",
        "index": str(index_path),
        "index_sha256": sha(index_path.read_bytes()),
        "package_sha256": index["public_archive"]["sha256"],
        "files": len(found),
        "bytes": bundle.stat().st_size,
        "groups": len(index["groups"]),
        "src_files": len(index["engine"]),
        "current_src_verified": current_src,
        **totals,
        "limits": "Integrity PASS preserves all recorded RED/fixture FAIL; it does not relabel them as successful behavior.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--current-src", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.verify, args.current_src) if args.verify else build(args.output)
    if args.report:
        args.report.write_bytes(encoded(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
