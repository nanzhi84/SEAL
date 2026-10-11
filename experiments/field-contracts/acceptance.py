"""Before implementation: real CLI → Scrapy → Archive → PG → export/Replay contracts."""

import argparse
import copy
import hashlib
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/v1.2-acceptance"))
from acceptance import Harness  # noqa: E402
from support import RECIPE, collect, manifest, pack, schema, source  # noqa: E402

MAPPING = {
    "Cơ quan ban hành:": "issuing_authority",
    "Năm ban hành:": "issued_year",
    "Lĩnh vực:": "subject_areas",
}


def exercise(h):
    bodies = {
        "a": {"Cơ quan ban hành:": "Thủ tướng Chính phủ", "Năm ban hành:": 2026, "Lĩnh vực:": None, "local": "kept"},
        "b": {"agency": "Thủ tướng Chính phủ", "tags": ["policy", "science"], "available": False},
        "parse": {"Cơ quan ban hành:": "Thủ tướng Chính phủ", "Năm ban hành:": "09-10-2026", "Lĩnh vực:": ["policy", "science"], "local": "kept"},
        "parse-failed": {"Cơ quan ban hành:": None, "Năm ban hành:": "uncertain", "Lĩnh vực:": None, "local": None},
        "transform-failed": {"Cơ quan ban hành:": "unknown-number", "Năm ban hành:": 2026, "Lĩnh vực:": None, "local": None},
    }
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET

    def get(request):
        path = request.path.removeprefix("/fields/")
        if path not in bodies:
            return original(request)
        body = json.dumps({"id": "one", "data": bodies[path]}, ensure_ascii=False).encode()
        h.site.ledger.append({"method": "GET", "path": request.path, "status": 200})
        request.send_response(200)
        request.send_header("Content-Type", "application/json; charset=utf-8")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = get
    try:
        config = manifest(mapping=MAPPING)
        version = pack(h, config)
        binding = source(h, "mapped", version)
        run, exported = collect(h, "mapped", binding)
        h.check("mapped_complete", run["status"], "complete")
        record = exported["records"][0]
        expected = {"issuing_authority": "Thủ tướng Chính phủ", "issued_year": 2026, "subject_areas": None, "local": "kept"}
        h.check("mapped_internal_data", record["data"], expected)
        h.check("precise_source_keys", record["extracted_data"], bodies["a"])
        h.check("unmapped_source_key_retained", record["data"]["local"], "kept")
        h.check("null_has_no_field_evidence", "subject_areas" in record["field_evidence"], False)
        h.check("null_has_no_status_or_reason", set(record["data"]), set(expected))
        h.check("contract_schema_id", record["data_schema_id"], "urn:seal:schema:a:v1:mapped")
        for target, evidence in record["field_evidence"].items():
            h.check("source_field_" + target, evidence["target_field"], target)
            raw = json.loads(archive(h, evidence["snapshot_id"]))
            body = json.loads(archive(h, raw["body_hash"]))
            h.check("independent_" + target, body["data"][evidence["source_field"]], evidence["raw_value"])
            h.check("rename_" + target, evidence["extracted_value"], record["data"][target])

        unmapped = pack(h, manifest(), "unmapped")
        _, native = collect(h, "native", source(h, "native", unmapped))
        h.check("no_mapping_retains_keys", native["records"][0]["data"], bodies["a"])

        b = {"family": "second", "entrypoint": "recipe:FieldsSpider", "params_schema": {"type": "object"}, "extraction_schema": schema("different:v1", {"agency": {"type": ["string", "null"]}, "tags": {"type": ["array", "null"], "items": {"type": "string"}}, "available": {"type": ["boolean", "null"]}})}
        bversion = pack(h, b, "second")
        _, different = collect(h, "different", source(h, "different", bversion, "b"))
        h.check("different_task_without_mapping", different["records"][0]["data"], bodies["b"])
        b["field_mapping"] = {"agency": "issuing_authority"}
        bversion = pack(h, b, "second-mapped")
        _, aligned = collect(h, "aligned", source(h, "aligned", bversion, "b"))
        h.check("second_source_shared_meaning", aligned["records"][0]["data"]["issuing_authority"], expected["issuing_authority"])
        h.check("multivalue_not_truncated", aligned["records"][0]["data"]["tags"], ["policy", "science"])

        parser = {"Năm ban hành:": {"entrypoint": "recipe:year", "params": {"format": "%d-%m-%Y"}}}
        rules = {"Cơ quan ban hành:": {"entrypoint": "recipe:prefix", "params": {"prefix": "AUTH:"}}}
        pv = pack(h, manifest(mapping=MAPPING, transforms=rules, parsers=parser), "parsed")
        _, parsed = collect(h, "parsed", source(h, "parsed", pv, "parse"))
        pr = parsed["records"][0]
        h.check("explicit_normalization", pr["data"]["issuing_authority"], "AUTH:Thủ tướng Chính phủ")
        h.check("parsed_year", pr["data"]["issued_year"], 2026)
        h.check("date_original_retained", pr["field_evidence"]["issued_year"]["raw_value"], "09-10-2026")
        h.check("multivalue_mapped", pr["data"]["subject_areas"], ["policy", "science"])
        _, failed = collect(h, "parse_failed", source(h, "parse_failed", pv, "parse-failed"))
        h.check("parse_failure_null", failed["records"][0]["data"], dict.fromkeys(expected))
        h.check("native_null_no_evidence", failed["records"][0]["field_evidence"], {})
        fconfig = manifest(mapping=MAPPING, transforms={"Cơ quan ban hành:": {"entrypoint": "recipe:integer"}})
        fconfig["data_schema"]["properties"]["issuing_authority"] = {"type": ["integer", "null"]}
        fv = pack(h, fconfig, "normalization-null")
        _, failed = collect(h, "normalization_null", source(h, "normalization_null", fv, "transform-failed"))
        h.check("normalization_failure_null", failed["records"][0]["data"]["issuing_authority"], None)
        h.check("normalization_prevalue_retained", failed["records"][0]["field_evidence"]["issuing_authority"]["extracted_value"], "unknown-number")

        for label, params, error in (
            ("missing_key", {"missing_key": "local"}, "field_extraction_schema_violation"),
            ("wrong_type", {"bad_type": "Cơ quan ban hành:"}, "field_extraction_schema_violation"),
            ("missing_locator", {"missing_locator": "Cơ quan ban hành:"}, "record_locator_fields_mismatch"),
            ("wrong_schema_id", {"schema_id": "urn:wrong:v1"}, "field_schema_version_mismatch"),
        ):
            rejected, output = collect(h, label, source(h, label, version, params=params), ok=False)
            h.check(label + "_no_records", output["records"], [])
            h.check(label + "_stable_error", error in output["run"]["errors"])

        invalid_configs(h, config)
        # Freeze all schema/mapping/function inputs; editing the local package cannot mutate old Binding.
        newer = copy.deepcopy(config)
        newer["field_mapping"]["Cơ quan ban hành:"] = "agency_v2"
        newer["extraction_schema"]["$id"] = "urn:seal:schema:a:v2"
        newer_version = pack(h, newer, "bundle")
        newer_binding = h.binding("mapped", newer_version)
        h.check("updated_binding_has_new_identity", newer_binding != binding)
        h.check("old_export_unchanged", h.cli("export", "mapped", "--run", run["run_id"])["records"][0]["data"], expected)
        before = len(h.site.ledger)
        replay = h.cli("replay", run["run_id"])
        replayed = h.cli("export", "mapped", "--run", replay["run_id"])
        h.check("historical_replay_zero_network", len(h.site.ledger), before)
        h.check("historical_replay_data", replayed["records"][0]["data"], expected)
        h.check("historical_replay_evidence", replayed["records"][0]["field_evidence"], record["field_evidence"])
        h.capture("historical-replay", {"receipt": replay, "export": replayed})
        # Existing no-contract package still follows original locator behavior and export shape.
        legacy = copy.deepcopy(config)
        for key in ("extraction_schema", "field_mapping"):
            legacy.pop(key)
        lv = pack(h, legacy, "legacy")
        # Historical validators require locators for JSON null, so use the non-null second source.
        _, old = collect(h, "legacy", source(h, "legacy", lv, "b"))
        h.check("legacy_data_unchanged", old["records"][0]["data"], bodies["b"])
        h.check("legacy_export_has_no_field_metadata", "field_evidence" in old["records"][0], False)
    finally:
        handler.do_GET = original


def invalid_configs(h, valid):
    variants = []
    for label, mapping in (
        ("unknown_source", {"absent": "target"}),
        ("empty_target", {"local": ""}),
        ("duplicate_target", {"local": "target", "Lĩnh vực:": "target"}),
        ("unmapped_collision", {"local": "Lĩnh vực:"}),
    ):
        value = copy.deepcopy(valid)
        value["field_mapping"] = mapping
        variants.append((label, value))
    for label, changed in (("unknown_dialect", {"$schema": "https://example.invalid/schema"}), ("invalid_schema", {"type": "nonsense"}), ("remote_reference", {"$ref": "https://example.invalid/schema"})):
        value = copy.deepcopy(valid)
        value["extraction_schema"].update(changed)
        variants.append((label, value))
    for label, rule in (("missing_callable", {"entrypoint": "recipe:missing"}), ("invalid_parameters", {"entrypoint": "recipe:year", "params": {"unexpected": 1}})):
        value = copy.deepcopy(valid)
        value["field_transforms"] = {"local": rule}
        variants.append((label, value))
    for label, value in variants:
        root = h.root / label
        root.mkdir()
        (root / "recipe.py").write_text(RECIPE)
        (root / "recipe.yaml").write_text(json.dumps(value))
        before = len(h.site.ledger)
        receipt = h.cli("recipe", "pack", root, ok=False)
        h.check(label + "_rejected", receipt.get("error", "").startswith("field_"))
        h.check(label + "_no_http", len(h.site.ledger), before)


def archive(h, key):
    body = (h.root / "archive/objects" / key[:2] / key[2:]).read_bytes()
    h.check("archive_hash_" + key, hashlib.sha256(body).hexdigest(), key)
    return body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live-vietnam", action="store_true")
    args = parser.parse_args()
    h, error = Harness(args.output), None
    try:
        h.start()
        exercise(h)
        from vietnam_acceptance import exercise_vietnam

        exercise_vietnam(h, live=args.live_vietnam)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("field-contracts", error)
        path = h.output / "manifest.json"
        value = json.loads(path.read_text())
        value.update(command="uv run --frozen python experiments/field-contracts/acceptance.py --output <new-directory>" + (" --live-vietnam" if args.live_vietnam else ""), scope="isolated PostgreSQL and two synthetic sources; archived Vietnam public detail" + (" and live Vietnam GET/field probe" if args.live_vietnam else ""))
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        summary = {"status": "FAIL" if error else "PASS", "assertions": len(h.assertions), "failed": sum(x["status"] != "PASS" for x in h.assertions), "command": value["command"], "scope": value["scope"]}
        (h.output / "summary.json").write_text(json.dumps(summary, indent=2))
        h.close()
    print(json.dumps(summary))
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
