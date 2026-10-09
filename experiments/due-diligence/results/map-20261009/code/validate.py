"""External artifact contract, written before crawler implementation."""

import argparse
import hashlib
import json
from pathlib import Path

from inventory import inventory, write_json


def validate(root):
    rows = inventory()
    results = json.loads((root / "results.json").read_text())
    ledger = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
    assertions = []

    def check(name, actual, expected=True):
        assertions.append(
            {
                "name": name,
                "expected": expected,
                "actual": actual,
                "status": "PASS" if actual == expected else "FAIL",
            }
        )

    check(
        "every_map_row_accounted", sorted(r["id"] for r in results), sorted(r["id"] for r in rows)
    )
    check("no_duplicate_result_rows", len({r["id"] for r in results}), len(results))
    check("no_pending_rows", all(r["outcome"] != "PENDING" for r in results))
    check("bounded_requests", len(ledger) <= 2400)
    check("unique_request_ids", len({r["request_id"] for r in ledger}), len(ledger))
    check("get_only", all(r["method"] == "GET" for r in ledger))
    for r in results:
        if r.get("raw_path"):
            path = root / r["raw_path"]
            check(
                "body_integrity_" + r["id"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
                r["body_sha256"],
            )
        if r["outcome"].startswith("ACCESSIBLE"):
            check(
                "nonempty_success_" + r["id"],
                r.get("http_status") == 200 and r.get("text_chars", 0) > 40,
            )
    serialized = json.dumps(results) + json.dumps(ledger)
    check("no_session_id_in_artifacts", ";jsessionid=" not in serialized.lower())
    check(
        "no_sensitive_headers",
        '"set-cookie"' not in serialized.lower() and '"authorization"' not in serialized.lower(),
    )
    write_json(root / "assertions.json", assertions)
    print(
        json.dumps(
            {
                "assertions": len(assertions),
                "failed": sum(a["status"] != "PASS" for a in assertions),
            }
        )
    )
    return any(a["status"] != "PASS" for a in assertions)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign", type=Path)
    args = parser.parse_args()
    raise SystemExit(validate(args.campaign))
