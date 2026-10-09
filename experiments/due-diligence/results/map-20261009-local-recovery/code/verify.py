"""Reproduce external artifact assertions against frozen real responses, offline."""

import argparse
import hashlib
import json
import socket
from pathlib import Path

from classify import extract
from content import compact, parse_content, response_for
from inventory import MAP, ROOT, inventory, write_json

HERE = Path(__file__).resolve().parent


def verify(output):
    network_attempts = []

    def forbidden(*args, **kwargs):
        network_attempts.append("blocked")
        raise RuntimeError("offline_verification_forbids_network")

    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden
    socket.getaddrinfo = forbidden
    root = HERE / "golden"
    fixtures = json.loads((root / "fixtures.json").read_text())
    rows = json.loads((root / "entries.json").read_text())
    checks = []

    def check(name, actual, expected=True):
        checks.append(
            {
                "name": name,
                "expected": expected,
                "actual": actual,
                "status": "PASS" if actual == expected else "FAIL",
            }
        )

    check("full_map_coverage", [r["id"] for r in rows], [r["id"] for r in inventory()])
    check("no_interrupted_final_rows", all(r["outcome"] != "INTERRUPTED" for r in rows))
    check("no_subject_queries", all(not r["query_submitted"] for r in rows))
    requests = []
    for campaign in sorted({r["campaign"] for r in rows}):
        path = ROOT / campaign
        manifest = json.loads((path / "manifest.json").read_text())
        check(
            "input_map_hash:" + campaign,
            hashlib.sha256(MAP.read_bytes()).hexdigest(),
            manifest["input_sha256"],
        )
        requests.extend(
            json.loads(line) for line in (path / "requests.jsonl").read_text().splitlines()
        )
    check("total_campaign_request_budget", len(requests) <= 2400)
    check("unique_requests", len({r["request_id"] for r in requests}), len(requests))
    check("GET_only", all(r["method"] == "GET" for r in requests))
    check(
        "no_footer_noise_in_golden",
        all(not r["map_noise"] for r in rows if r["id"] in {f["id"] for f in fixtures}),
    )
    for fixture in fixtures:
        key = fixture["id"]
        try:
            response = response_for(fixture)
            check(
                key + ":body_sha256",
                hashlib.sha256(response.body).hexdigest(),
                fixture["body_sha256"],
            )
            parsed = extract(response)
            for field, expected in fixture["expected_entry"].items():
                check(key + ":entry:" + field, parsed.get(field), expected)
            check(key + ":entry_links", parsed["links"], fixture["expected_links"])
            if fixture["tier"] != "content":
                continue
            spec = fixture["spec"]
            parsed = parse_content(response, spec)
            frozen = json.loads((root / "extracted" / (key + ".json")).read_text())
            check(key + ":saved_extraction", parsed == frozen)
            if spec["kind"] == "records":
                records = parsed["records"]
                check(key + ":record_count", len(records), spec["expected_count"])
                for end, index in (("first", 0), ("last", -1)):
                    for field, expected in spec.get("expected_" + end, {}).items():
                        actual = records[index].get(field) if records else None
                        check(key + ":" + end + ":" + field, actual, expected)
            elif spec["kind"] == "document":
                check(key + ":title", parsed["title"], spec["expected_title"])
                check(
                    key + ":attachments", len(parsed["attachments"]), spec["expected_attachments"]
                )
                for value in spec["body_contains"]:
                    check(key + ":body_contains:" + value, value in compact(parsed["body"]))
            else:
                pages = parsed["pages"]
                check(key + ":page_count", len(pages), spec["expected_pages"])
                check(key + ":all_pages_have_text", all(p.strip() for p in pages))
                for end, index in (("first", 0), ("last", -1)):
                    for value in spec[end + "_contains"]:
                        check(key + ":" + end + ":" + value, value in compact(pages[index]))
        except Exception as exc:
            check(key + ":parse_completed", type(exc).__name__, "no_exception")
    for row in json.loads((root / "negative-cases.json").read_text()):
        response = response_for(row)
        check(
            row["id"] + ":negative_body_hash",
            hashlib.sha256(response.body).hexdigest(),
            row["body_sha256"],
        )
        check(
            row["id"] + ":JS_shell_not_success",
            extract(response)["outcome"],
            row["expected_outcome"],
        )
    check("zero_network_attempts", len(network_attempts), 0)
    failed = [c for c in checks if c["status"] == "FAIL"]
    result = {
        "command": "uv run --frozen python experiments/due-diligence/verify.py",
        "environment": "uv.lock; no database/account/query inputs; frozen local application bodies",
        "scope": "entry parsing baselines and 7 reviewed content samples; not live source or full site acceptance",
        "checks": checks,
        "summary": {
            "assertions": len(checks),
            "passed": len(checks) - len(failed),
            "failed": len(failed),
            "fixtures": len(fixtures),
            "content_fixtures": sum(f["tier"] == "content" for f in fixtures),
            "network_attempts": len(network_attempts),
        },
    }
    write_json(output, result)
    print(json.dumps(result["summary"]))
    for fail in failed:
        print(json.dumps(fail, ensure_ascii=False))
    return bool(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(verify(args.output))
