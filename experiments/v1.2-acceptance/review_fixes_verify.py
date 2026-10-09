"""Read-only independent evidence review; final status waits for the master package."""

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path.cwd()
RESULTS = ROOT / "experiments/v1.2-acceptance/results/review-fixes-final"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--evidence-root", type=Path, default=ROOT)
parser.add_argument("--index", type=Path, default=RESULTS / "review-evidence-index.json")
parser.add_argument("--output", type=Path, default=RESULTS / "independent-review.json")
args = parser.parse_args()
EVIDENCE_ROOT, OUT = args.evidence_root.resolve(), args.output.resolve()
checks, errors = 0, []


def check(name, condition):
    global checks
    checks += 1
    if not condition:
        errors.append(name)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(path.read_text())


def resolve(value, pointer):
    check("pointer_prefix", not pointer or pointer.startswith("/"))
    for token in pointer[1:].split("/") if pointer else []:
        check("pointer_escape", re.search(r"~(?![01])", token) is None)
        token = token.replace("~1", "/").replace("~0", "~")
        if type(value) is list:
            check("pointer_index", re.fullmatch(r"0|[1-9][0-9]*", token) is not None)
            value = value[int(token)]
        else:
            value = value[token]
    return value


def equivalent(actual, expected):
    return type(actual) is type(expected) and json.dumps(actual, sort_keys=True) == json.dumps(
        expected, sort_keys=True
    )


def manifest(root, current=True):
    value = load(root / "manifest.json")
    for name, expected in value["files"].items():
        check(str(root) + ":file:" + name, sha(root / name) == expected)
    if current:
        sources = {
            str(p) for p in Path("src").rglob("*") if p.is_file() and "__pycache__" not in p.parts
        }
        check(
            str(root) + ":engine_set", sources == {p for p in value["code"] if p.startswith("src/")}
        )
        for name in sources:
            check(str(root) + ":engine:" + name, sha(Path(name)) == value["code"][name])
    assertions = load(root / "assertions.json")
    check(str(root) + ":assertions", all(row["status"] == "PASS" for row in assertions))
    return {
        "directory": str(root),
        "manifest_sha256": sha(root / "manifest.json"),
        "assertions": len(assertions),
        "files": len(value["files"]),
        "current_engine": current,
    }


def raw(root, identity):
    path = root / "archive/objects" / identity[:2] / identity[2:]
    value = path.read_bytes()
    check("archive_digest:" + identity, hashlib.sha256(value).hexdigest() == identity)
    return value


def projection(root, capture, count, fields=None):
    records = capture["export"]["records"]
    check("business_record_count", len(records) == count)
    bodies = {}
    for entry in capture["inspect"]["run"]["inputs"]:
        snapshot = json.loads(raw(root, entry["snapshot_id"]))
        check("public_get200", snapshot["method"] == "GET" and snapshot["status"] == 200)
        bodies[entry["snapshot_id"]] = json.loads(raw(root, snapshot["body_hash"]))
    for record in records:
        result = record["result_evidence"][0]
        document = bodies[result["primary_inputs"][0]["snapshot_id"]]
        if fields:
            check("exact_fields", set(record["data"]) == set(fields))
        for field, value in record["data"].items():
            locator = result["locators"][field]
            target = bodies[locator.get("snapshot_id", result["primary_inputs"][0]["snapshot_id"])]
            check("native_field", equivalent(value, resolve(target, locator["pointer"])))
        located_key = resolve(document, result["key_locator"]["pointer"])
        check("identity", str(located_key) == record["record_key"])
    return {r["record_key"]: r["data"] for r in records}


def performance():
    root = EVIDENCE_ROOT / "artifacts/acceptance/v1.2-json-pointer-final"
    baseline_root = EVIDENCE_ROOT / "artifacts/acceptance/v1.2-json-pointer-baseline"
    provenance = manifest(root)
    historical = manifest(baseline_root, False)
    baseline, final = load(baseline_root / "summary.json"), load(root / "summary.json")
    check("performance_same_dataset", baseline["dataset"] == final["dataset"])
    check("performance_same_environment", baseline["environment"] == final["environment"])
    ratios = []
    for index, baseline_index in [(0, 0), (2, 1)]:
        before, after = baseline["timings"][baseline_index], final["timings"][index]
        cpu = after["cpu_seconds"] / before["cpu_seconds"]
        rss = after["peak_rss_bytes"] / before["peak_rss_bytes"]
        check("measured_cpu_reduction", cpu <= 0.65)
        check("measured_rss_bound", rss <= 1.5)
        ratios.append(
            {
                "operation": after["args"][0],
                "before_cpu_seconds": before["cpu_seconds"],
                "after_cpu_seconds": after["cpu_seconds"],
                "cpu_ratio": cpu,
                "rss_ratio": rss,
            }
        )
    online_ids = None
    for label, revision in [("collect-first", 0), ("collect-changed", 1), ("replay-original", 0)]:
        value = load(root / (label + ".json"))
        business = projection(root, value, 1000)
        for key, row in business.items():
            number = int(key.removeprefix("public-"))
            check(
                "1000x10_semantics",
                row
                == {
                    "id": f"public-{number:04}",
                    "name": f"Published business entity {number}",
                    "active": number % 2 == 0,
                    "count": number + revision,
                    "score": number + 0.5,
                    "optional": None,
                    "tags": ["public", str(number)],
                    "address": {"city": "Singapore", "rank": number},
                    "a/b": f"Slash key {number}",
                    "til~de": f"Tilde key {number}",
                },
            )
        stats = value["inspect"]["run"]["report"]["stats"]
        check("one_validation_decode", stats["seal/record_json_parses"] == 1)
        check("999_same_input_hits", stats["seal/record_json_hits"] == 999)
        check(
            "released_cache",
            stats["seal/record_json_retained_bytes"]
            == stats["seal/record_json_retained_entries"]
            == 0,
        )
        check("actual_decoded_tree_bound", stats["seal/record_json_peak_bytes"] <= 64 * 1024 * 1024)
        identities = {r["record_key"]: r["record_id"] for r in value["export"]["records"]}
        if label == "collect-first":
            online_ids = identities
        elif label == "collect-changed":
            check("changed_bytes_online_ids_stable", identities == online_ids)
        else:
            check("replay_no_fetch", value["inspect"]["observations"] == [])
            check("replay_http_zero", value["inspect"]["discovery"]["http_attempts"] == 0)
    return {
        **provenance,
        "baseline": historical,
        "measured_ratios": ratios,
        "records": 3000,
        "native_fields": 30000,
        "limits": [
            "One measured invocation per comparable path; no production throughput claim",
            "Final engine includes P1 fixes; this compares full CLI behavior, not a pure isolated cache experiment",
            "64 MiB bounds retained decoded trees, not all temporary validation or downloader memory",
            "LRU eviction loop reviewed statically; cumulative multi-input eviction is not an additional E2E claim",
        ],
    }


def boundaries():
    root = EVIDENCE_ROOT / "artifacts/acceptance/v1.2-json-pointer-boundaries-complete"
    provenance = manifest(root)
    for label, count in [("oversized", 1), ("oversized-replay", 1), ("nohash-two-inputs", 2)]:
        value = load(root / (label + ".json"))
        projection(root, value, count)
        stats = value["inspect"]["run"]["report"]["stats"]
        check(
            "boundary_released",
            stats["seal/record_json_retained_bytes"]
            == stats["seal/record_json_retained_entries"]
            == 0,
        )
        check(
            "boundary_decode_count",
            stats["seal/record_json_parses"] == (1 if label.startswith("oversized") else 2),
        )
        if label.startswith("oversized"):
            check("oversized_not_retained", stats["seal/record_json_peak_bytes"] == 0)
        if label == "oversized-replay":
            check("boundary_offline", value["inspect"]["observations"] == [])
    summary = load(root / "summary.json")
    check("boundary_summary_pass", summary["status"] == "PASS")
    check(
        "oversized_finite_fixture_rss", summary["timings"][0]["peak_rss_bytes"] < 768 * 1024 * 1024
    )
    return {
        **provenance,
        "records": 4,
        "native_fields": 40,
        "peak_rss_bytes": summary["timings"][0]["peak_rss_bytes"],
        "failed_fixture_scope_preserved": "artifacts/acceptance/v1.2-json-pointer-boundaries-final",
    }


def pagination():
    root = EVIDENCE_ROOT / "artifacts/acceptance/v1.2-amac-full-final"
    provenance = manifest(root)
    synthetic = manifest(EVIDENCE_ROOT / "artifacts/acceptance/v1.2-pagination-final")
    source = "full_dd_250"
    expected_urls = {
        f"https://www.amac.org.cn/portal/front/financial/fundTrustee/findFundTrusteesPage?pageNo={n}&pageSize=10"
        for n in range(1, 8)
    }
    before_values = before_ids = None
    rounds = []
    for label in ["first", "second", "recheck", "replay"]:
        capture = load(root / (source + "-" + label + ".json"))
        business = projection(root, capture, 67, ["trustName", "regAddr"])
        run = capture["inspect"]["run"]
        check("amac_complete", run["status"] == "complete")
        check("full7_urls", {i["url"] for i in run["inputs"]} == expected_urls)
        pages, names = [], set()
        for entry in run["inputs"]:
            snapshot = json.loads(raw(root, entry["snapshot_id"]))
            envelope = json.loads(raw(root, snapshot["body_hash"]))
            payload = envelope["data"]["data"]
            number = int(parse_qs(urlsplit(entry["url"]).query)["pageNo"][0])
            check("native_total67", type(payload["total"]) is int and payload["total"] == 67)
            check(
                "native_codes",
                type(envelope["code"]) is int
                and envelope["code"] == 200
                and type(envelope["data"]["errcode"]) is int
                and envelope["data"]["errcode"] == 0,
            )
            check("page_lengths", len(payload["dataList"]) == (7 if number == 7 else 10))
            for row in payload["dataList"]:
                check("unique_business_name", row["trustName"] not in names)
                names.add(row["trustName"])
            pages.append(number)
        check("all_pages_1_to_7", sorted(pages) == list(range(1, 8)))
        check("all67_source_names", set(business) == names and len(names) == 67)
        records = capture["export"]["records"]
        check(
            "parents_preserved",
            all(
                r["detail_url"] is None
                and r["frozen_parent_request"]["role"] == "api"
                and r["frozen_parent_request"]["url"] in expected_urls
                for r in records
            ),
        )
        ids = {r["record_key"]: r["record_id"] for r in records}
        if before_values is None:
            before_values, before_ids = business, ids
        else:
            check("same_all67_business_values", business == before_values)
            if label != "replay":
                check("same_all67_online_ids", ids == before_ids)
        observations = capture["inspect"]["observations"]
        check("request_boundary", {x["url"] for x in observations} <= expected_urls)
        check("network_count", len(observations) == (0 if label == "replay" else 7))
        if label == "recheck":
            check("parent_seeds_exact7", {s["url"] for s in run["seeds"]} == expected_urls)
            check("planned67_record_scope", len(run["recheck_plan"]["records"]) == 67)
            check("native_chain_dedup", capture["inspect"]["discovery"]["deduplicated"] >= 6)
        if label == "replay":
            check("amac_replay_http0", capture["inspect"]["discovery"]["http_attempts"] == 0)
        rounds.append(
            {
                "round": label,
                "records": len(records),
                "pages": sorted(pages),
                "http_attempts": len(observations),
            }
        )
    return {
        **provenance,
        "synthetic": synthetic,
        "rounds": rounds,
        "source_id": source,
        "recipe_version": capture["inspect"]["binding"]["recipe_version"]
        if "binding" in capture["inspect"]
        else records[0]["recipe_version"],
        "limits": [
            "No upstream snapshot token; atomic cross-page snapshot is unproven",
            "Source identity is displayed trustName; a rename creates a new identity",
            "No page8 HTTP in the four acceptance runs; a prior public empty-page probe is separate",
        ],
    }


def root_regressions():
    roots = {
        name: EVIDENCE_ROOT / "artifacts/acceptance" / folder
        for name, folder in {
            "joint": "v1.2-review-joint-final",
            "migration": "v1.2-review-m3-final-v2",
            "real_abc": "v1.2-review-real-abc-final",
            "smoke": "v1.2-review-smoke-final-v2",
            "proxy": "v1.2-review-proxy-final",
        }.items()
    }
    evidence = {name: manifest(root) for name, root in roots.items()}
    history = load(roots["migration"] / "migration-history.json")
    check("M3_all7_old_tables", len(history["before"]) == 7)
    check("M3_all_pre_upgrade_values_unchanged", history["before"] == history["after"])
    smoke = load(roots["smoke"] / "summary.json")
    check(
        "smoke_all33_cases",
        len(smoke["cases"]) == 33 and all(c["status"] == "PASS" for c in smoke["cases"]),
    )
    cases = load(roots["smoke"] / "cases.json")
    historical = next(c for c in cases if c["id"] == "C01")
    old_values = next(
        a for a in historical["assertions"] if a["name"] == "historical_old_columns_unchanged"
    )
    check("C01_all8_old_tables", len(old_values["expected"]) == 8)
    check("C01_every_old_field_same", old_values["actual"] == old_values["expected"])
    check(
        "C01_public_migrate_before_replay",
        any(
            a["name"] == "historical_public_migration" and a["actual"] == "v1.2"
            for a in historical["assertions"]
        ),
    )
    evidence["migration"].update(old_tables=7, pre_upgrade_values_unchanged=True)
    evidence["smoke"].update(cases=33, historical_old_tables=8, old_fields_unchanged=True)
    evidence["failed_evidence_preserved"] = ["v1.2-review-m3-final", "v1.2-review-smoke-final"]
    return evidence


def master_package():
    index_path = args.index.resolve()
    if not index_path.exists():
        return {"status": "PENDING", "reason": "Master package/index not yet delivered"}
    index = load(index_path)
    archive = index_path.parent / index["public_archive"]["path"]
    check("master_tar_sha256", sha(archive) == index["public_archive"]["sha256"])
    check("master_tar_bytes", archive.stat().st_size == index["public_archive"]["bytes"])
    check(
        "master_current_engine_set",
        set(index["engine"])
        == {str(p) for p in Path("src").rglob("*") if p.is_file() and "__pycache__" not in p.parts},
    )
    for name, expected in index["engine"].items():
        check("master_current_engine:" + name, sha(Path(name)) == expected)
    prefix = index["archive_prefix"] + "/"
    with tarfile.open(archive, "r:gz") as bundle:
        members = {m.name: m for m in bundle.getmembers() if m.isfile()}
        check("master_member_set", set(members) == {prefix + name for name in index["files"]})
        for name, expected in index["files"].items():
            check("safe_tar_path", not Path(name).is_absolute() and ".." not in Path(name).parts)
            data = bundle.extractfile(members[prefix + name]).read()
            check("master_member_digest:" + name, hashlib.sha256(data).hexdigest() == expected)
        check("no_tar_links", all(not m.issym() and not m.islnk() for m in bundle.getmembers()))
    groups = index["groups"]
    positives = [g for g in groups if g["category"] == "positive_current_engine"]
    for group in positives:
        check("master_positive_engine:" + group["id"], group["current_engine"] is True)
        check("master_positive_assertions:" + group["id"], group["live_assertions"]["failed"] == 0)
        check("master_positive_functional:" + group["id"], group["functional_status"] == "PASS")
        check(
            "master_group_manifest:" + group["id"],
            sha(EVIDENCE_ROOT / group["path"] / "manifest.json") == group["manifest_sha256"],
        )
    check("master_positive_not_empty", bool(positives))
    check(
        "master_all_diagnostic_categories_preserved",
        {"behavior_red", "performance_red", "historical_input"} <= {g["category"] for g in groups},
    )
    return {
        "status": "PASS" if not errors else "FAIL",
        "index_sha256": sha(index_path),
        "public_archive_sha256": sha(archive),
        "bytes": archive.stat().st_size,
        "members": len(index["files"]),
        "positive_groups": [g["id"] for g in positives],
        "categories": {
            category: sum(g["category"] == category for g in groups)
            for category in sorted({g["category"] for g in groups})
        },
        "pre_package_review": "experiments/v1.2-acceptance/results/review-fixes-final/pre-package-independent-review.json",
        "note": "Final review is an external sidecar to avoid embedding its own package hash",
    }


result = {
    "artifact_version": 1,
    "review_status": "PENDING_MASTER_PACKAGE",
    "json_cache": performance(),
    "cache_boundaries": boundaries(),
    "full_pagination": pagination(),
    "root_regressions": root_regressions(),
    "code_review": {
        "status": "PASS",
        "findings": [],
        "raw_integrity": "validate_record reads and SHA-verifies each snapshot/body before cache access; cache hit cannot replace Objects.get",
        "native_semantics": "Strict decoder rejects nonfinite/duplicate object keys; typed recursive equality and strict RFC6901 indices/escapes remain",
        "lifetime": "Per ItemPipeline locked LRU; actual decoded tree sizes; oversized local-only validation reuse; close clears all retained trees",
    },
    "master_package": master_package(),
    "checks": checks,
    "errors": errors,
    "reproduce": "uv run --frozen python experiments/v1.2-acceptance/review_fixes_verify.py",
    "evidence_root": str(EVIDENCE_ROOT),
}
if errors:
    result["review_status"] = "FAIL"
elif result["master_package"]["status"] == "PASS":
    result["review_status"] = "PASS"
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"checks": checks, "errors": errors, "status": result["review_status"]}))
raise SystemExit(1 if errors else 0)
