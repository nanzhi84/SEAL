"""Recompute real full-directory termination from frozen public API envelopes."""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from live_verify import object_bytes, verify_export


def verify(root, samples):
    contract = json.loads(samples.read_text())
    if len(contract) != 1:
        raise ValueError("one_source_contract_required")
    sample = contract[0]
    source = sample["source"]["id"]
    total = sample["params"]["expected_total"]
    count = max(1, (total + 9) // 10)
    expected_urls = set(sample["expected"]["archived_urls"])
    manifest = json.loads(root.joinpath("manifest.json").read_text())
    checks, rounds = [], []

    def check(name, actual, expected=True):
        checks.append(
            {
                "name": name,
                "actual": actual,
                "expected": expected,
                "status": "PASS" if actual == expected else "FAIL",
            }
        )

    check(
        "frozen_samples_hash",
        manifest["input_samples"][str(samples)],
        hashlib.sha256(samples.read_bytes()).hexdigest(),
    )
    check("live_harness_pass", manifest["real_source_acceptance"]["status"], "PASS")
    baseline = None
    for label in ("first", "second", "recheck", "replay"):
        value = json.loads(root.joinpath(f"{source}-{label}.json").read_text())
        receipt, inspected, exported = value["receipt"], value["inspect"], value["export"]
        prefix = label + "_"
        check(prefix + "complete", receipt["status"], "complete")
        inputs = inspected["run"]["inputs"]
        check(prefix + "unique_page_inputs", len(inputs), count)
        check(
            prefix + "all_declared_urls", sorted({x["url"] for x in inputs}), sorted(expected_urls)
        )
        oracle, pages = {}, []
        for ref in inputs:
            snapshot = json.loads(object_bytes(root / "archive", ref["snapshot_id"]))
            body = object_bytes(root / "archive", snapshot["body_hash"])
            envelope = json.loads(body)
            payload = envelope["data"]["data"]
            query = parse_qs(urlsplit(ref["url"]).query)
            page = int(query["pageNo"][0])
            check(prefix + f"page{page}_size", query["pageSize"], ["10"])
            check(prefix + f"page{page}_native_total", type(payload["total"]) is int)
            check(prefix + f"page{page}_total", payload["total"], total)
            check(
                prefix + f"page{page}_envelope",
                [envelope["code"], envelope["data"]["errcode"]],
                [200, 0],
            )
            rows = payload["dataList"]
            check(
                prefix + f"page{page}_exact_rows",
                len(rows),
                min(10, max(total - (page - 1) * 10, 0)),
            )
            check(
                prefix + f"page{page}_archived_get",
                [snapshot["method"], snapshot["status"]],
                ["GET", 200],
            )
            for row in rows:
                check(prefix + f"key_unique_{row['trustName']}", row["trustName"] not in oracle)
                oracle[row["trustName"]] = {k: row[k] for k in ("trustName", "regAddr")}
            pages.append(
                {
                    "page": page,
                    "rows": len(rows),
                    "total": payload["total"],
                    "body_hash": snapshot["body_hash"],
                    "snapshot_id": ref["snapshot_id"],
                    "url": ref["url"],
                }
            )
        check(
            prefix + "page_set_including_terminal",
            sorted(x["page"] for x in pages),
            list(range(1, count + 1)),
        )
        check(prefix + "unique_natural_key_count", len(oracle), total)
        actual = {r["record_key"]: r["data"] for r in exported["records"]}
        check(prefix + "exact_record_count", len(exported["records"]), total)
        check(prefix + "raw_business_values", actual, oracle)
        check(prefix + "raw_field_key_locators", verify_export(root / "archive", exported), [])
        check(
            prefix + "api_parent_without_detail",
            all(
                r["detail_url"] is None
                and r["frozen_parent_request"]["role"] == "api"
                and r["frozen_parent_request"]["method"] == "GET"
                and r["frozen_parent_request"]["url"] in expected_urls
                for r in exported["records"]
            ),
        )
        if baseline is None:
            baseline = actual
        else:
            check(prefix + "same_full_business_set", actual, baseline)
        observations = inspected["observations"]
        check(
            prefix + "actual_network_page_count",
            len(observations),
            0 if label == "replay" else count,
        )
        check(prefix + "no_extra_page_http", {x["url"] for x in observations} <= expected_urls)
        if label == "recheck":
            check("recheck_all_frozen_api_seeds", len(inspected["run"]["seeds"]), count)
            check("recheck_native_chain_dedup", inspected["discovery"]["deduplicated"] >= count - 1)
        if label == "replay":
            check("replay_zero_http_attempts", inspected["discovery"]["http_attempts"], 0)
        rounds.append(
            {
                "round": label,
                "run_id": receipt["run_id"],
                "pages": sorted(pages, key=lambda x: x["page"]),
            }
        )
    # JSON values keep the proof portable; do not depend on SEAL's Record parser.
    return {
        "artifact_version": 1,
        "status": "PASS" if all(x["status"] == "PASS" for x in checks) else "FAIL",
        "source_id": source,
        "directory": str(root),
        "expected_total": total,
        "terminal_page": count,
        "checks": checks,
        "rounds": rounds,
        "limits": [
            "No API snapshot token: cross-page atomicity unproven",
            "Displayed-name changes form a new Source identity",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.directory.resolve(), args.samples)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, default=sorted) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(
        json.dumps(
            {
                "status": result["status"],
                "checks": len(result["checks"]),
                "failed": [x["name"] for x in result["checks"] if x["status"] != "PASS"],
            }
        )
    )
    return int(result["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
