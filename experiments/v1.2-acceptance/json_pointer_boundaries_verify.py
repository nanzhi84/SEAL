"""Recompute boundary CLI evidence offline without importing the runtime."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit


def resolve(document, pointer):
    value = document
    for token in pointer[1:].split("/") if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def expected(number):
    return {
        "id": f"boundary-{number}",
        "name": f"Published boundary entity {number}",
        "active": number % 2 == 0,
        "count": number,
        "score": number + 0.5,
        "optional": None,
        "tags": ["public", str(number)],
        "address": {"city": "Singapore", "rank": number},
        "a/b": f"Slash key {number}",
        "til~de": f"Tilde key {number}",
    }


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False)


def verify(root, current_code=False):
    checks, errors = 0, []

    def check(name, condition):
        nonlocal checks
        checks += 1
        if not condition:
            errors.append(name)

    def read(key):
        body = (root / "archive/objects" / key[:2] / key[2:]).read_bytes()
        check("archive_hash_" + key, hashlib.sha256(body).hexdigest() == key)
        return body

    manifest = json.loads((root / "manifest.json").read_text())
    for name, sha in manifest["files"].items():
        check("file_hash_" + name, hashlib.sha256((root / name).read_bytes()).hexdigest() == sha)
    if current_code:
        current = {
            str(path)
            for path in Path("src").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        frozen = {name for name in manifest["code"] if name.startswith("src/")}
        check("current_engine_file_set", current == frozen)
        for name in frozen:
            check(
                "current_engine_hash_" + name,
                hashlib.sha256(Path(name).read_bytes()).hexdigest() == manifest["code"][name],
            )
    summary = json.loads((root / "summary.json").read_text())
    assertions = json.loads((root / "assertions.json").read_text())
    check("live_assertions_pass", all(row["status"] == "PASS" for row in assertions))
    check("summary_pass", summary["status"] == "PASS")
    check("summary_count", summary["assertions"]["total"] == len(assertions))
    records, fields, native_tree_min_bytes = 0, 0, 0
    diagnostics = {}
    for phase, numbers in (
        ("oversized", [0]),
        ("oversized-replay", [0]),
        ("nohash-two-inputs", [1, 2]),
    ):
        evidence = json.loads((root / (phase + ".json")).read_text())
        receipt, inspection, export = (evidence[key] for key in ("receipt", "inspect", "export"))
        check(
            phase + "_complete",
            receipt["status"]
            == inspection["run"]["status"]
            == export["run"]["status"]
            == "complete",
        )
        check(phase + "_no_errors", receipt["errors"] == [])
        expected_keys = {f"boundary-{number}" for number in numbers}
        check(
            phase + "_exact_keys", {row["record_key"] for row in export["records"]} == expected_keys
        )
        check(
            phase + "_exact_count",
            len(inspection["record_results"]) == len(export["records"]) == len(numbers),
        )
        documents = {}
        for entry in inspection["run"]["inputs"]:
            snapshot_key = entry["snapshot_id"]
            snapshot = json.loads(read(snapshot_key))
            body = read(snapshot["body_hash"])
            path = urlsplit(snapshot["url"]).path
            check(
                phase + "_frozen_body_" + path,
                hashlib.sha256(body).hexdigest() == summary["fixtures"][path]["sha256"],
            )
            check(
                phase + "_bounded_body_" + path,
                len(body) == summary["fixtures"][path]["bytes"] and len(body) <= 30000000,
            )
            check(
                phase + "_get_200_" + path,
                snapshot["method"] == "GET" and snapshot["status"] == 200,
            )
            documents[snapshot_key] = json.loads(body)
        if phase == "oversized":
            document = next(iter(documents.values()))
            integers = document["unselected_public_integer_envelope"]
            check(
                "large_input_actual_integer_population",
                len(integers) == 2000000
                and all(value == index for index, value in enumerate(integers)),
            )
            native_tree_min_bytes = sys.getsizeof(integers) + sum(
                sys.getsizeof(value) for value in integers
            )
            check(
                "large_input_exceeds_tree_retention_budget",
                native_tree_min_bytes > 64 * 1024 * 1024,
            )
        for result in inspection["record_results"]:
            candidate = result["candidate"]
            key = candidate["record_key"]
            check(phase + "_known_key_" + key, key in expected_keys)
            number = int(key.removeprefix("boundary-"))
            check(
                phase + "_business_data_" + key,
                encoded(candidate["data"]) == encoded(expected(number)),
            )
            check(phase + "_ten_fields_" + key, len(candidate["data"]) == 10)
            document = documents[candidate["primary_snapshot_id"]]
            check(
                phase + "_key_pointer_" + key,
                str(resolve(document, candidate["key_locator"]["pointer"])) == key,
            )
            for field, actual in candidate["data"].items():
                value = resolve(document, candidate["locators"][field]["pointer"])
                check(
                    phase + "_typed_field_" + key + "_" + field,
                    type(actual) is type(value) and encoded(actual) == encoded(value),
                )
                fields += 1
            records += 1
        stats = inspection["run"]["report"]["stats"]
        check(
            phase + "_resources_released",
            stats["seal/record_json_retained_entries"]
            == stats["seal/record_json_retained_bytes"]
            == 0,
        )
        check(
            phase + "_bounded_retention", stats["seal/record_json_peak_bytes"] <= 64 * 1024 * 1024
        )
        diagnostics[phase] = {
            name: value for name, value in stats.items() if name.startswith("seal/record_json_")
        }
        if phase == "oversized-replay":
            check("oversized_replay_zero_observations", inspection["observations"] == [])
    ledger = json.loads((root / "request-ledger.json").read_text())
    check(
        "exact_three_business_http_requests",
        len(ledger) == 3 and {row["path"] for row in ledger} == set(summary["fixtures"]),
    )
    check(
        "finite_cli_process_memory",
        all(row["peak_rss_bytes"] < 768 * 1024 * 1024 for row in summary["timings"]),
    )
    return {
        "artifact": str(root.resolve()),
        "manifest_sha256": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
        "files": len(manifest["files"]),
        "records": records,
        "typed_fields": fields,
        "native_tree_min_bytes": native_tree_min_bytes,
        "diagnostics": diagnostics,
        "assertions": checks,
        "passed": checks - len(errors),
        "errors": errors,
        "current_engine_verified": current_code,
        "status": "PASS" if not errors else "FAIL",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--current-code", action="store_true")
    args = parser.parse_args()
    result = verify(args.artifact, args.current_code)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
