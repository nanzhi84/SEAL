"""Offline independent oracle: archived bytes/Schema/mapping/values, never PASS assertions."""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import jsonschema
from lxml import html


def equal(left, right):
    return type(left) is type(right) and (
        left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
        if isinstance(left, dict)
        else len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right, strict=True))
        if isinstance(left, list)
        else left == right
    )


def object_bytes(root, key):
    body = (root / "archive/objects" / key[:2] / key[2:]).read_bytes()
    assert hashlib.sha256(body).hexdigest() == key, "archive_digest_mismatch"
    return body


def object_json(root, key):
    return json.loads(object_bytes(root, key))


def located(root, snapshot_id, locator):
    if locator["kind"] == "values":
        return [located(root, part["snapshot_id"], part) for part in locator["locators"]]
    snapshot = object_json(root, snapshot_id)
    body = object_bytes(root, snapshot["body_hash"])
    if locator["kind"] == "json":
        value = json.loads(body)
        for token in locator["pointer"][1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            value = value[int(token)] if isinstance(value, list) else value[token]
        return value
    if locator["kind"] == "xpath":
        doc = html.fromstring(body.decode(snapshot["encoding"] or "utf-8"))
        nodes = doc.xpath(locator["path"])
        assert len(nodes) == 1, "locator_not_unique"
        return (
            " ".join(nodes[0].itertext()).strip()
            if hasattr(nodes[0], "itertext")
            else str(nodes[0]).strip()
        )
    raise AssertionError("oracle_locator_unsupported")


def transform(value, rule):
    name = rule["entrypoint"].split(":")[1]
    params = rule.get("params", {})
    try:
        if name in ("year", "issued_year"):
            return datetime.strptime(value, params["format"]).year
        if name == "prefix":
            return params["prefix"] + value
        if name == "integer":
            return int(value)
        if name == "subject_list":
            return value if isinstance(value, list) else [value] if value.strip() else None
    except (ValueError, TypeError, OverflowError):
        return None
    raise AssertionError("oracle_unknown_fixture_rule")


def record(root, value):
    payload = {key: value[key] for key in ("record_type", "schema_version", "data")}
    if "data_schema_id" in value:
        payload["data_schema_id"] = value["data_schema_id"]
    identity = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
    assert identity == value["content_hash"], "schema_semantic_content_hash_mismatch"
    bundle = object_json(root, value["recipe_version"])
    manifest = bundle["manifest"]
    if "extraction_schema" not in manifest:
        return 0
    extracted = value["extracted_data"]
    extraction_schema = manifest["extraction_schema"]
    jsonschema.validators.validator_for(extraction_schema)(extraction_schema).validate(extracted)
    assert set(extracted) == set(extraction_schema["properties"]), "source_keys_missing"
    mapping = manifest.get("field_mapping", {})
    rules = manifest.get("field_transforms", {})
    parsers = manifest.get("field_parsers", {})
    assert value["extraction_schema_id"] == extraction_schema["$id"]
    data_schema = manifest.get("data_schema")
    if data_schema is None:
        data_schema = json.loads(json.dumps(extraction_schema))
        if mapping:
            data_schema["$id"] += ":mapped"
            data_schema["properties"] = {
                mapping.get(k, k): v for k, v in data_schema["properties"].items()
            }
            data_schema["required"] = [mapping.get(k, k) for k in data_schema.get("required", [])]
    assert value["data_schema_id"] == data_schema["$id"], "data_schema_identity_mismatch"
    jsonschema.validators.validator_for(data_schema)(data_schema).validate(value["data"])
    expected = {}
    checks = 0
    for source, original in extracted.items():
        target = mapping.get(source, source)
        expected[target] = (
            transform(original, rules[source])
            if source in rules and original is not None
            else original
        )
        if original is None:
            assert target not in value["field_evidence"], "null_side_evidence"
            continue
        proof = value["field_evidence"][target]
        assert proof["source_field"] == source and proof["target_field"] == target
        assert proof["snapshot_id"] in value["snapshot_ids"], "unapproved_field_input"
        raw = located(root, proof["snapshot_id"], proof["locator"])
        assert equal(raw, proof["raw_value"]), "raw_value_mismatch"
        assert proof.get("parser") == parsers.get(source), "parser_not_frozen"
        parsed = transform(raw, proof["parser"]) if proof.get("parser") else raw
        assert equal(parsed, original) and equal(original, proof["extracted_value"]), (
            "extraction_not_reproducible"
        )
        assert proof.get("transform") == rules.get(source), "transform_not_frozen"
        for extra in proof.get("supporting_locators", []):
            assert extra["snapshot_id"] in value["snapshot_ids"]
            assert equal(located(root, extra["snapshot_id"], extra), extra["value"]), (
                "supporting_locator_mismatch"
            )
            checks += 1
        checks += 1
    assert equal(expected, value["data"]), "mapping_not_reproducible"
    for evidence in value["result_evidence"]:
        assert evidence["field_evidence"] == value["field_evidence"], "export_evidence_mismatch"
    return checks


def verify(root, vietnam_only=False):
    root = Path(root)
    checks, records, replays = 0, 0, 0
    for path in sorted(root.glob("*.json")):
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or "export" not in value:
            continue
        for row in value["export"]["records"]:
            checks += record(root, row)
            records += 1
        receipt = value.get("receipt", {})
        if receipt.get("mode") == "replay":
            report = value["export"]["run"]["report"]
            assert report.get("stats", {}).get("downloader/request_count", 0) == 0, "replay_network"
            assert report["discovery"]["http_attempts"] == 0, "replay_http_attempts"
            replays += 1
    if not vietnam_only:
        assert records >= 12 and replays >= 2, "required_scenarios_missing"
        mapped = json.loads((root / "mapped.json").read_text())["export"]["records"][0]
        assert mapped["data"] == {
            "subject_areas": None,
            "issuing_authority": "Thủ tướng Chính phủ",
            "issued_year": 2026,
            "local": "kept",
        }
        native = json.loads((root / "different.json").read_text())["export"]["records"][0]
        assert native["data"] == {
            "agency": "Thủ tướng Chính phủ",
            "tags": ["policy", "science"],
            "available": False,
        }
        for label in ("missing_key", "wrong_type", "missing_locator", "wrong_schema_id"):
            rejected = json.loads((root / (label + ".json")).read_text())["export"]
            assert rejected["records"] == [] and rejected["run"]["errors"], (
                "invalid_record_admitted"
            )
    # Explicit source-visible business expectations, independent from receipt/Schema PASS.
    for label, authority, areas in (
        ("vietnam-48", "Thủ tướng Chính phủ", None),
        ("vietnam-335", "Chính phủ", ["Chính sách xã hội"]),
    ):
        row = json.loads((root / (label + ".json")).read_text())["export"]["records"][0]
        assert row["data"] == {
            "issuing_authority": authority,
            "issued_year": 2026,
            "subject_areas": areas,
        }
        assert row["field_evidence"]["issued_year"]["supporting_locators"]
    if vietnam_only:
        assert records >= 7 and replays >= 2, "live_vietnam_scenarios_missing"
        live = json.loads((root / "vietnam-live.json").read_text())["export"]
        expected = {
            "48/2026/QĐ-TTg": {
                "issuing_authority": "Thủ tướng Chính phủ",
                "issued_year": 2026,
                "subject_areas": None,
            },
            "335/2026/NĐ-CP": {
                "issuing_authority": "Chính phủ",
                "issued_year": 2026,
                "subject_areas": ["Chính sách xã hội"],
            },
        }
        assert {row["record_key"]: row["data"] for row in live["records"]} == expected
        for row in live["records"]:
            assert any(
                object_json(root, identity)["method"] == "POST" for identity in row["snapshot_ids"]
            )
    return {
        "status": "PASS",
        "records": records,
        "field_recomputations": checks,
        "offline_replays": replays,
        "oracle": "archive SHA256, standalone lxml/JSON/date values and frozen bundle schemas/mapping; assertions.json unused",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--only-vietnam", action="store_true")
    args = parser.parse_args()
    try:
        result = verify(args.output, args.only_vietnam)
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "reason": type(exc).__name__, "detail": str(exc)}))
        return 1
    (args.output / "independent-verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
