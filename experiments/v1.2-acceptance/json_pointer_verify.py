"""Independent offline verification of benchmark files, types, keys and raw bytes."""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def resolve(document, pointer):
    current = document
    for token in pointer[1:].split("/") if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def expected(key, revision):
    number = int(key.removeprefix("public-"))
    return {
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
    }


def verify(root, baseline=None, current_code=False):
    checks, errors = 0, []

    def check(name, condition):
        nonlocal checks
        checks += 1
        if not condition:
            errors.append(name)

    def read(key):
        data = (root / "archive/objects" / key[:2] / key[2:]).read_bytes()
        check("object_hash_" + key, hashlib.sha256(data).hexdigest() == key)
        return data

    manifest = json.loads((root / "manifest.json").read_text())
    for filename, sha in manifest["files"].items():
        check(
            "file_hash_" + filename,
            hashlib.sha256((root / filename).read_bytes()).hexdigest() == sha,
        )
    if current_code:
        current_files = {
            str(path)
            for path in Path("src").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        check(
            "current_engine_file_set",
            current_files
            == {filename for filename in manifest["code"] if filename.startswith("src/")},
        )
        for filename, sha in manifest["code"].items():
            if not filename.startswith("src/"):
                continue
            check(
                "current_engine_" + filename,
                hashlib.sha256(Path(filename).read_bytes()).hexdigest() == sha,
            )
    assertions = json.loads((root / "assertions.json").read_text())
    check("stored_assertions_all_pass", all(row["status"] == "PASS" for row in assertions))
    summary = json.loads((root / "summary.json").read_text())
    check("summary_success", summary["status"] == "PASS")
    check("summary_assertion_count", summary["assertions"]["total"] == len(assertions))
    phases = [("collect-first", 0), ("replay-original", 0)]
    if (root / "collect-changed.json").exists():
        phases.insert(1, ("collect-changed", 1))
    identities, total_fields, total_records = {}, 0, 0
    for phase, revision in phases:
        evidence = json.loads((root / (phase + ".json")).read_text())
        receipt, inspection, exported = (evidence[key] for key in ("receipt", "inspect", "export"))
        namespace = inspection["run"]["namespace"]
        current_identities = identities.setdefault(namespace, {})
        check(
            phase + "_complete",
            receipt["status"]
            == inspection["run"]["status"]
            == exported["run"]["status"]
            == "complete",
        )
        results = inspection["record_results"]
        check(phase + "_exact_count", len(results) == len(exported["records"]) == 1000)
        check(
            phase + "_unique_keys",
            {row["record_key"] for row in exported["records"]}
            == {f"public-{number:04}" for number in range(1000)},
        )
        check(phase + "_one_input", len(inspection["run"]["inputs"]) == 1)
        snapshot_key = inspection["run"]["inputs"][0]["snapshot_id"]
        snapshot = json.loads(read(snapshot_key))
        raw = read(snapshot["body_hash"])
        check(phase + "_get_200", snapshot["method"] == "GET" and snapshot["status"] == 200)
        if revision == 0:
            check(
                phase + "_frozen_bytes",
                hashlib.sha256(raw).hexdigest() == summary["dataset"]["original_sha256"],
            )
        document = json.loads(raw)
        native_rows = document["data"]["data"]["dataList"]
        check(phase + "_raw_population", len(native_rows) == 1000)
        check(
            phase + "_raw_oracle",
            {row["id"]: encoded(row) for row in native_rows}
            == {
                f"public-{i:04}": encoded(expected(f"public-{i:04}", revision)) for i in range(1000)
            },
        )
        for result in results:
            candidate = result["candidate"]
            key = candidate["record_key"]
            check(phase + "_input_" + key, result["inputs"] == [snapshot_key])
            check(
                phase + "_business_data_" + key,
                encoded(candidate["data"]) == encoded(expected(key, revision)),
            )
            check(
                phase + "_key_locator_" + key,
                str(resolve(document, candidate["key_locator"]["pointer"])) == key,
            )
            check(phase + "_ten_fields_" + key, len(candidate["data"]) == 10)
            for field, value in candidate["data"].items():
                located = resolve(document, candidate["locators"][field]["pointer"])
                check(
                    phase + "_typed_field_" + key + "_" + field,
                    type(located) is type(value) and encoded(located) == encoded(value),
                )
                total_fields += 1
            total_records += 1
        for row in exported["records"]:
            key = row["record_key"]
            check(
                phase + "_export_" + key, encoded(row["data"]) == encoded(expected(key, revision))
            )
            if key in current_identities:
                check(
                    phase + "_stable_identity_" + key, current_identities[key] == row["record_id"]
                )
            else:
                current_identities[key] = row["record_id"]
        if phase == "replay-original":
            check("replay_zero_observations", inspection["observations"] == [])
    ledger = json.loads((root / "request-ledger.json").read_text())
    check("exact_http_count", len(ledger) == (2 if len(phases) == 3 else 1))
    check(
        "exact_http_scope",
        all(row["path"] == "/api/performance" and row["status"] == 200 for row in ledger),
    )
    if baseline:
        before = json.loads(baseline.read_text())
        check("identical_performance_dataset", before["dataset"] == summary["dataset"])
        check(
            "frozen_performance_contract",
            before["performance_contract"] == summary["performance_contract"],
        )
        for kind in ("run", "replay"):
            old = next(row for row in before["timings"] if row["args"][0] == kind)
            new = next(row for row in summary["timings"] if row["args"][0] == kind)
            check(
                kind + "_cpu_contract",
                new["cpu_seconds"]
                <= old["cpu_seconds"] * summary["performance_contract"]["cpu_ratio_max"],
            )
            check(
                kind + "_rss_contract",
                new["peak_rss_bytes"]
                <= old["peak_rss_bytes"] * summary["performance_contract"]["rss_ratio_max"],
            )
    return {
        "artifact": str(root.resolve()),
        "manifest_sha256": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
        "files": len(manifest["files"]),
        "records": total_records,
        "typed_fields": total_fields,
        "assertions": checks,
        "errors": errors,
        "passed": checks - len(errors),
        "current_engine_verified": current_code,
        "status": "PASS" if not errors else "FAIL",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--current-code", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.artifact, args.baseline, args.current_code)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
