"""Issue #1 T7S gate over actual, independently verified real-source runs.

Counts and mechanism evidence are separate. A passing sample suite cannot
satisfy missing iframe coverage; diagnostic-only sources never count as adapted.
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from live_verify import object_bytes, verify_export

ROOT = Path(__file__).resolve().parents[2]


def verify(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    checks, samples, mechanisms = [], [], {}

    def check(name, actual, expected=True):
        checks.append(
            dict(
                name=name,
                actual=actual,
                expected=expected,
                status="PASS" if actual == expected else "FAIL",
            )
        )

    check("real_suite_pass", manifest["real_source_acceptance"]["status"], "PASS")
    for name, digest in manifest["files"].items():
        path = (directory / name).resolve()
        check(
            "file_hash:" + name,
            path.is_relative_to(directory.resolve())
            and path.is_file()
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest,
        )
    for name, digest in manifest["code"].items():
        if name.startswith(("src/", "recipes/")):
            check(
                "current_source:" + name,
                hashlib.sha256((ROOT / name).read_bytes()).hexdigest(),
                digest,
            )
    for sample in manifest["real_source_acceptance"]["samples"]:
        if sample["status"] != "PASS" or sample.get("runtime_status") != "complete":
            continue
        rid, source = sample["research_id"], sample["source_id"]
        config = json.loads((directory / (source + "-configuration.json")).read_text())
        rounds = {}
        for label in ("first", "second", "recheck", "replay"):
            value = json.loads((directory / f"{source}-{label}.json").read_text())
            exported, inspected = value["export"], value["inspect"]
            check(rid + ":" + label + ":complete", value["receipt"]["status"], "complete")
            check(
                rid + ":" + label + ":raw_fields",
                verify_export(directory / "archive", exported),
                [],
            )
            check(rid + ":" + label + ":quality", exported["quality_status"], "not_evaluated")
            if label == "replay":
                check(rid + ":replay:zero_http", inspected["discovery"]["http_attempts"], 0)
            rounds[label] = {
                "run_id": value["receipt"]["run_id"],
                "status": value["receipt"]["status"],
                "records": len(exported["records"]),
                "http_attempts": inspected["discovery"]["http_attempts"],
                "raw_hashes": sorted(
                    {ref["body_hash"] for record in exported["records"] for ref in record["inputs"]}
                ),
                "artifact": f"{source}-{label}.json",
            }
            if label != "first":
                continue
            records = exported["records"]
            events = inspected["discovery"]["events"]
            evidence = {
                "source": rid,
                "run_id": value["receipt"]["run_id"],
                "artifact": f"{source}-{label}.json",
            }
            roles = {
                event["role"] for event in events if event["archived_at"] and event["parsed_at"]
            }
            if {"list", "detail"} <= roles:
                mechanisms.setdefault("html_list_detail", []).append(evidence)
            if "attachment" in roles and any(
                event["parent_snapshot_id"] for event in events if event["role"] == "attachment"
            ):
                mechanisms.setdefault("business_attachment", []).append(evidence)
            if "iframe" in roles and any(
                event["parent_snapshot_id"] for event in events if event["role"] == "iframe"
            ):
                mechanisms.setdefault("static_business_iframe", []).append(evidence)
            pages = {
                event["url"]
                for event in events
                if event["archived_at"] and event["role"] in {"api", "list", "iframe"}
            }
            if len(pages) >= 2:
                mechanisms.setdefault("pagination", []).append(evidence)
            if (
                "api" in roles
                and len(records) > 1
                and all(record["detail_url"] is None for record in records)
            ):
                mechanisms.setdefault("multi_record_json_no_detail", []).append(evidence)
            if "fixed_public_query" in config["expected"].get("mechanisms", []):
                query = parse_qs(urlsplit(config["source"]["entry_urls"][0]).query)
                if query.get("houseName") == [config["params"].get("house_name")] or query.get("q"):
                    mechanisms.setdefault("fixed_public_query", []).append(evidence)
            for observation in inspected["observations"]:
                if observation["snapshot_id"]:
                    snapshot = json.loads(
                        object_bytes(directory / "archive", observation["snapshot_id"])
                    )
                    object_bytes(directory / "archive", snapshot["body_hash"])
        recheck = json.loads((directory / f"{source}-recheck.json").read_text())
        if recheck["inspect"]["discovery"]["deduplicated"] > 0:
            mechanisms.setdefault("native_deduplication", []).append(
                {"source": rid, **rounds["recheck"]}
            )
        samples.append({**sample, "rounds": rounds})
    counts = Counter(sample["class"] for sample in samples)
    for category in "ABC":
        check("T7S:" + category + ":two_sources", counts[category] >= 2)
    for mechanism in (
        "html_list_detail",
        "pagination",
        "native_deduplication",
        "multi_record_json_no_detail",
        "fixed_public_query",
        "static_business_iframe",
        "business_attachment",
    ):
        check("T7S:" + mechanism, bool(mechanisms.get(mechanism)))
    return {
        "status": "PASS" if all(row["status"] == "PASS" for row in checks) else "FAIL",
        "quality_status": "not_evaluated",
        "git_head": manifest["git_head"],
        "counts": dict(counts),
        "samples": samples,
        "mechanisms": mechanisms,
        "assertions": checks,
        "scope": "Issue #1 T7S; six counts alone do not satisfy mechanisms",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.directory)
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: result[key] for key in ("status", "counts", "git_head")}))
    return int(result["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
