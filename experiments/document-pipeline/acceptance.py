"""Real URL → Scrapy → Archive → unified document → optional Record contracts."""

import argparse
import copy
import hashlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
V12 = ROOT / "experiments/v1.2-acceptance"
sys.path.insert(0, str(V12))
SPEC = importlib.util.spec_from_file_location("seal_document_acceptance", V12 / "acceptance.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
Harness = MODULE.Harness

from fixtures import JSON_VALUE, resources  # noqa: E402
from runtime_support import sql  # noqa: E402
from support import by_url, collect, pack, source  # noqa: E402

TYPES = {"article", "image", "table", "json", "document_ref"}


def archived(h, key):
    body = (h.root / "archive/objects" / key[:2] / key[2:]).read_bytes()
    h.check("archive_digest_" + key, hashlib.sha256(body).hexdigest(), key)
    return body


def content_without_foreign_keys(document):
    nodes = copy.deepcopy(document["children"])
    for node in nodes:
        if node["type"] == "document_ref":
            node.pop("document_id", None)
    return nodes


def exercise(h):
    fixtures = resources(h.site.pdf, h.site.docx, h.site.xls)
    h.capture(
        "fixture-oracle",
        {
            path: {"content_type": content_type, "body_sha256": hashlib.sha256(body).hexdigest()}
            for path, (content_type, body) in fixtures.items()
        },
    )
    handler, original = h.site.server.RequestHandlerClass, h.site.server.RequestHandlerClass.do_GET

    def get(request):
        if request.path not in fixtures:
            return original(request)
        content_type, body = fixtures[request.path]
        h.site.ledger.append({"method": "GET", "path": request.path, "status": 200})
        request.send_response(200)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = get
    try:
        version = pack(h)
        binding = source(h, "documents", version, ["/documents/list"])
        run, exported, _ = collect(h, "documents", binding, label="all-formats")
        documents = by_url(exported)
        h.check("document_only_complete", run["status"], "complete")
        h.check("all_urls_are_saved_including_list_pages", len(documents), 15)
        normal = h.cli("export", "documents", "--run", run["run_id"])
        h.check("document_only_has_no_business_records", normal["records"], [])
        h.capture("document-only-business-view", normal)
        for url, document in documents.items():
            path = url.removeprefix(h.site.url)
            h.check(path + "_single_document_model", document["type"], "document")
            h.check(
                path + "_only_five_content_types",
                {n["type"] for n in document["children"]} <= TYPES,
            )
            h.check(
                path + "_one_full_article",
                sum(n["type"] == "article" for n in document["children"]) <= 1,
            )
            snapshot = h.cli("inspect", "snapshot", document["snapshot_id"])
            h.check(
                path + "_raw_original_bytes",
                hashlib.sha256(archived(h, snapshot["snapshot"]["body_hash"])).hexdigest(),
                hashlib.sha256(fixtures[path][1]).hexdigest(),
            )
            query = h.cli("inspect", "document-version", document["document_version_id"])
            h.check(path + "_version_query_children", query["children"], document["children"])
            h.check(path + "_recipe_provenance", document["recipe_version"], version)
            h.check(path + "_binding_provenance", document["binding_id"], binding)
            h.check(path + "_has_actual_observation", bool(document["observation_id"]))
            h.check(path + "_has_parser_identity", bool(document["parser_version"]))
            h.capture("snapshot-" + document["snapshot_id"], snapshot)
        notice = documents[h.site.url + "/documents/notice"]
        h.check(
            "html_dom_content_order",
            [n["type"] for n in notice["children"]],
            ["article", "image", "table", "document_ref"],
        )
        text = notice["children"][0]["text"]
        h.check(
            "html_article_contains_whole_body",
            "Before the resources." in text and "After the resources." in text,
        )
        h.check(
            "html_real_table_cells",
            notice["children"][2]["rows"],
            [["Name", "Amount"], ["Public entity", "42"]],
        )
        h.check(
            "html_image_keeps_resource_url",
            notice["children"][1]["url"],
            h.site.url + "/documents/image.png",
        )
        xlsx = documents[h.site.url + "/documents/shared.xlsx"]
        h.check(
            "xlsx_sheets_are_parallel_tables",
            [n["type"] for n in xlsx["children"]],
            ["table", "table"],
        )
        h.check("xlsx_sheet_names", [n["name"] for n in xlsx["children"]], ["First", "Second"])
        h.check(
            "xlsx_native_scalar_cells",
            xlsx["children"][0]["rows"],
            [["name", "amount"], ["Public entity", 42]],
        )
        h.check(
            "xlsx_merged_empty_positions",
            xlsx["children"][1]["rows"],
            [["Merged", None], [True, None]],
        )
        csv = documents[h.site.url + "/documents/public.csv"]["children"]
        h.check("csv_is_one_real_table", len(csv), 1)
        h.check(
            "csv_quoting_and_empty_cells",
            csv[0]["rows"],
            [
                ["name", "amount", "comment"],
                ["Public, entity", "42", 'quoted "value"'],
                ["Empty", "", ""],
            ],
        )
        for path, value in (
            ("data.json", JSON_VALUE),
            ("array.json", [1, {"kind": "public"}, None]),
            ("scalar.json", "Public scalar"),
            ("null.json", None),
        ):
            nodes = documents[h.site.url + "/documents/" + path]["children"]
            h.check(path + "_complete_json_root", nodes, [{"type": "json", "value": value}])
        h.check(
            "pdf_real_text_layer",
            documents[h.site.url + "/documents/report.pdf"]["children"][0]["text"],
            "Synthetic PDF notice",
        )
        docx = documents[h.site.url + "/documents/report.docx"]["children"]
        h.check("docx_has_article_and_table", {n["type"] for n in docx}, {"article", "table"})
        h.check(
            "docx_whole_article",
            "行业分类合成公开样本" in docx[0]["text"] and "表格之后的解释文字" in docx[0]["text"],
        )
        h.check(
            "xls_every_sheet_is_table",
            all(
                n["type"] == "table"
                for n in documents[h.site.url + "/documents/workbook.xls"]["children"]
            ),
        )
        image = documents[h.site.url + "/documents/image.png"]["children"]
        h.check("standalone_image_document", image[0]["type"], "image")
        h.check(
            "standalone_image_archived_reference",
            image[0]["body_hash"],
            hashlib.sha256(fixtures["/documents/image.png"][1]).hexdigest(),
        )
        h.check(
            "plain_text_article",
            documents[h.site.url + "/documents/plain.txt"]["children"][0]["text"],
            "Plain notice\nPublic plain body",
        )
        shared_references = [
            row["references"][0] for row in (notice, documents[h.site.url + "/documents/also"])
        ]
        h.check(
            "shared_refs_resolve_same_document_foreign_key",
            {ref["document_id"] for ref in shared_references},
            {xlsx["id"]},
        )
        h.check(
            "shared_refs_are_queryably_parsed",
            {ref["state"] for ref in shared_references},
            {"parsed"},
        )
        h.check(
            "shared_refs_keep_name_and_relationship",
            all(ref["name"] and ref["relation"] == "attachment" for ref in shared_references),
        )
        h.check(
            "shared_target_downloaded_once",
            sum(r["path"] == "/documents/shared.xlsx" for r in h.site.ledger),
            1,
        )
        document_query = h.cli("inspect", "document", xlsx["id"])
        h.check("target_foreign_key_resolves", document_query["document"]["url"], xlsx["url"])
        h.capture("shared-document-query", document_query)

        # Repeated inputs and new parser rules have distinct, externally visible semantics.
        _, repeated, _ = collect(h, "documents", binding, label="repeat-collect")
        h.check(
            "repeat_uses_same_document_versions",
            {url: d["document_version_id"] for url, d in by_url(repeated).items()},
            {url: d["document_version_id"] for url, d in documents.items()},
        )
        h.check(
            "repeat_retains_new_observations",
            all(
                by_url(repeated)[url]["inputs"][0]["observation_id"]
                != doc["inputs"][0]["observation_id"]
                for url, doc in documents.items()
            ),
        )
        before = len(h.site.ledger)
        replay = h.cli("replay", run["run_id"])
        replayed = h.cli("export", "documents", "--documents", "--run", replay["run_id"])
        h.capture("all-formats-replay", {"receipt": replay, "export": replayed})
        h.check("all_formats_replay_zero_network", len(h.site.ledger), before)
        h.check(
            "all_formats_replay_zero_http_attempts", replay["summary"]["counts"]["http_attempts"], 0
        )
        h.check("all_formats_replay_complete", replay["status"], "complete")
        h.check(
            "all_formats_replay_same_content",
            {url: content_without_foreign_keys(d) for url, d in by_url(replayed).items()},
            {url: content_without_foreign_keys(d) for url, d in documents.items()},
        )
        direct = source(h, "direct", version, ["/documents/plain.txt"])
        first, first_export, _ = collect(h, "direct", direct, label="direct-first")
        fixtures["/documents/plain.txt"] = ("text/plain", b"Plain notice\nUpdated public body")
        changed, changed_export, _ = collect(h, "direct", direct, label="changed-input")
        h.check(
            "changed_input_new_version",
            first_export["documents"][0]["document_version_id"]
            != changed_export["documents"][0]["document_version_id"],
        )
        h.check(
            "changed_input_new_revision",
            first_export["documents"][0]["revision_id"]
            != changed_export["documents"][0]["revision_id"],
        )
        _, rechecked, inspected = collect(h, "direct", direct, recheck=True, label="direct-recheck")
        h.check(
            "recheck_has_one_frozen_target",
            inspected["run"]["recheck_plan"]["unique_request_count"],
            1,
        )
        h.check(
            "recheck_uses_unchanged_version",
            rechecked["documents"][0]["document_version_id"],
            changed_export["documents"][0]["document_version_id"],
        )
        rules_binding = h.binding("direct", version, {"options": {"format": "txt"}})
        _, rules_export, _ = collect(h, "direct", rules_binding, label="parser-rule-update")
        h.check(
            "parser_rules_create_new_version",
            rules_export["documents"][0]["document_version_id"]
            != changed_export["documents"][0]["document_version_id"],
        )
        old = h.cli("export", "direct", "--documents", "--run", first["run_id"])
        h.check(
            "historical_run_preserves_old_content",
            old["documents"][0]["children"],
            first_export["documents"][0]["children"],
        )
        before = len(h.site.ledger)
        old_replay = h.cli("replay", first["run_id"])
        old_replay_export = h.cli("export", "direct", "--documents", "--run", old_replay["run_id"])
        h.check("changed_live_body_historical_replay_zero_network", len(h.site.ledger), before)
        h.check(
            "changed_live_body_historical_replay_preserves_old_input",
            old_replay_export["documents"][0]["children"],
            first_export["documents"][0]["children"],
        )
        h.capture(
            "changed-history-replay",
            {"receipt": old_replay, "export": old_replay_export, "changed": changed},
        )
        failures(h, version, fixtures)
        business(h)
        sample_recipe(h, fixtures)
        legacy(h)
    finally:
        handler.do_GET = original


def failures(h, version, fixtures):
    paths = [
        "/documents/" + path
        for path in ("bad.pdf", "bad.docx", "bad.xlsx", "bad.json", "unsupported.bin")
    ]
    binding = source(h, "invalid_files", version, paths)
    run, output, inspected = collect(h, "invalid_files", binding, ok=False)
    h.check("invalid_files_do_not_forge_documents", output["documents"], [])
    h.check("invalid_files_have_explicit_errors", len(run["report"]["errors"]) >= 5)
    for event in inspected["discovery"]["events"]:
        if event["snapshot_id"]:
            snapshot = h.cli("inspect", "snapshot", event["snapshot_id"])
            h.check(
                "invalid_input_archive_stays_available",
                snapshot["archive"]["availability"],
                "available",
            )
            h.check(
                "invalid_input_original_bytes",
                hashlib.sha256(archived(h, snapshot["snapshot"]["body_hash"])).hexdigest(),
                hashlib.sha256(fixtures[event["url"].removeprefix(h.site.url)][1]).hexdigest(),
            )
    for defect, error in (
        ("node", "invalid_document_node"),
        ("foreign_key", "invalid_document_reference"),
        ("input", "document_lineage_mismatch"),
    ):
        name = "invalid_" + defect
        binding = source(h, name, version, ["/documents/plain.txt"], {"defect": defect})
        receipt, exported, _ = collect(h, name, binding, ok=False)
        h.check(name + "_not_saved", exported["documents"], [])
        h.check(name + "_stable_error", error in receipt["report"]["errors"])
    binding = source(h, "failed_page_discovery", version, ["/documents/access-denied"])
    _, exported, _ = collect(h, "failed_page_discovery", binding, ok=False)
    h.check(
        "failed_page_still_discovers_details",
        h.site.url + "/documents/plain.txt" in by_url(exported),
    )
    h.check(
        "access_denied_is_not_successful_document",
        h.site.url + "/documents/access-denied" in by_url(exported),
        False,
    )
    binding = source(h, "refs", version, ["/documents/ref-parent"])
    receipt, exported, _ = collect(h, "refs", binding, ok=False)
    parent = by_url(exported)[h.site.url + "/documents/ref-parent"]
    states = {ref["url"].rsplit("/", 1)[-1]: ref["state"] for ref in parent["references"]}
    h.check("bad_parse_target_ref_is_parse_failed", states["bad.pdf"], "parse_failed")
    h.check("not_scheduled_target_ref_is_not_fetched", states["not-fetched.txt"], "not_fetched")
    h.check("failed_target_ref_is_not_successful", states["missing.txt"] != "parsed")
    h.check(
        "unfetched_target_does_not_attempt_http",
        any(r["path"] == "/documents/not-fetched.txt" for r in h.site.ledger),
        False,
    )
    for ref in parent["references"]:
        target = h.cli("inspect", "document", ref["document_id"])
        h.check("unavailable_ref_foreign_key_still_resolves", target["document"]["url"], ref["url"])
    before = len(h.site.ledger)
    replay = h.cli("replay", receipt["run_id"], ok=False)
    h.check("failed_refs_replay_zero_network", len(h.site.ledger), before)
    h.check(
        "failed_refs_replay_has_no_http_attempts", replay["summary"]["counts"]["http_attempts"], 0
    )
    h.capture("failed-refs-replay", replay)


def business(h):
    version = pack(h, "business-recipe", business=True)
    binding = source(h, "business", version, ["/documents/data.json"], {"extract_business": True})
    run, documents, _ = collect(h, "business", binding)
    exported = h.cli("export", "business", "--run", run["run_id"])
    record = exported["records"][0]
    h.capture("business-record", exported)
    h.check("optional_business_complete", run["status"], "complete")
    h.check(
        "business_mapping_and_explicit_transform",
        record["data"],
        {"issued_year": "2026", "issuing_authority": None},
    )
    h.check(
        "business_null_has_no_status_or_evidence", set(record["field_evidence"]), {"issued_year"}
    )
    h.check(
        "business_raw_value_retained", record["field_evidence"]["issued_year"]["raw_value"], 2026
    )
    versions = record["document_versions"]
    h.check(
        "business_uses_saved_document_version",
        versions[0]["document_version_id"],
        documents["documents"][0]["document_version_id"],
    )
    candidate = sql(
        h, "SELECT candidate FROM seal_record_result WHERE id=%s", (record["record_result_id"],)
    )[0]["candidate"]
    h.check(
        "business_locator_proves_saved_value",
        candidate["locators"]["year"],
        {
            "kind": "document",
            "version_id": versions[0]["document_version_id"],
            "pointer": "/children/0/value/year",
        },
    )
    before = len(h.site.ledger)
    replay = h.cli("replay", run["run_id"])
    replayed = h.cli("export", "business", "--run", replay["run_id"])
    h.check("business_replay_zero_network", len(h.site.ledger), before)
    h.check("business_replay_mapping_and_null", replayed["records"][0]["data"], record["data"])
    h.capture("business-replay", {"receipt": replay, "export": replayed})
    previous_check = sql(
        h, "SELECT next_check FROM seal_record WHERE id=%s", (record["record_id"],)
    )[0]["next_check"]
    recheck, _, inspected = collect(h, "business", binding, recheck=True, label="business-recheck")
    current_check = sql(
        h, "SELECT next_check FROM seal_record WHERE id=%s", (record["record_id"],)
    )[0]["next_check"]
    h.check(
        "business_recheck_record_is_planned", len(inspected["run"]["recheck_plan"]["records"]), 1
    )
    h.check("business_recheck_advances_record_next_check", current_check > previous_check)
    h.check(
        "business_recheck_emits_record",
        len(h.cli("export", "business", "--run", recheck["run_id"])["records"]),
        1,
    )
    changed_binding = h.binding(
        "business", version, {"extract_business": True, "options": {"format": "json"}}
    )
    changed, changed_documents, _ = collect(
        h, "business", changed_binding, label="business-parser-rule-update"
    )
    changed_record = h.cli("export", "business", "--run", changed["run_id"])["records"][0]
    h.check("business_parser_rule_preserves_values", changed_record["data"], record["data"])
    h.check(
        "business_parser_rule_creates_new_document_version",
        changed_documents["documents"][0]["document_version_id"]
        != documents["documents"][0]["document_version_id"],
    )
    h.check(
        "business_parser_rule_creates_new_record_version",
        changed_record["record_version_id"] != record["record_version_id"],
    )
    h.capture("business-parser-rule-record", changed_record)
    nested_version = pack(h, "nested-document-recipe", business=True, nested=True)
    nested_binding = source(
        h,
        "nested_document_locator",
        nested_version,
        ["/documents/data.json"],
        {"extract_business": True, "defect": "nested_locator"},
    )
    rejected, _, _ = collect(h, "nested_document_locator", nested_binding, ok=False)
    rejected_records = h.cli("export", "nested_document_locator", "--run", rejected["run_id"])
    h.check("nested_document_locator_has_no_records", rejected_records["records"], [])
    h.check(
        "nested_document_locator_requires_linked_version",
        "record_document_locator_unlinked" in rejected["report"]["errors"],
    )


def legacy(h):
    version = h.cli("recipe", "pack", "recipes/heterogeneous")["recipe_version"]
    h.config(
        "legacy_records",
        entries=[h.site.url + "/api"],
        output_schema="record.v1",
        identity="business_key",
        seed_role="api",
        delay=0.0,
    )
    binding = h.binding("legacy_records", version, {"mode": "json", "json_scope": "single_page"})
    run = h.cli("collect", "legacy_records", "--binding", binding)
    exported = h.cli("export", "legacy_records", "--run", run["run_id"])
    h.check("legacy_records_still_export", len(exported["records"]), 10)
    h.check(
        "legacy_records_no_document_contract_added",
        all("document_versions" not in row for row in exported["records"]),
    )
    frozen = copy.deepcopy(exported["records"])
    before = len(h.site.ledger)
    replay = h.cli("replay", run["run_id"])
    replayed = h.cli("export", "legacy_records", "--run", replay["run_id"])
    h.check("legacy_replay_zero_network", len(h.site.ledger), before)
    h.check(
        "legacy_replay_same_business_values",
        [r["data"] for r in replayed["records"]],
        [r["data"] for r in frozen],
    )
    h.check(
        "legacy_historical_export_unchanged",
        h.cli("export", "legacy_records", "--run", run["run_id"])["records"],
        frozen,
    )
    h.capture(
        "legacy-compatibility",
        {"receipt": run, "export": exported, "replay": replay, "replay_export": replayed},
    )


def sample_recipe(h, fixtures):
    version = h.cli("recipe", "pack", "recipes/documents")["recipe_version"]
    binding = source(h, "sample_documents", version, ["/documents/notice"], robots=True)
    run, exported, _ = collect(h, "sample_documents", binding)
    h.check("sample_recipe_default_document_only_complete", run["status"], "complete")
    h.check(
        "sample_recipe_follows_real_attachment",
        h.site.url + "/documents/shared.xlsx" in by_url(exported),
    )
    h.check(
        "sample_recipe_default_no_business_records",
        h.cli("export", "sample_documents", "--run", run["run_id"])["records"],
        [],
    )
    before = len(h.site.ledger)
    replay = h.cli("replay", run["run_id"])
    replayed = h.cli("export", "sample_documents", "--documents", "--run", replay["run_id"])
    h.check("sample_recipe_document_replay_zero_network", len(h.site.ledger), before)
    h.check(
        "sample_recipe_document_replay_preserves_content",
        {url: content_without_foreign_keys(row) for url, row in by_url(replayed).items()},
        {url: content_without_foreign_keys(row) for url, row in by_url(exported).items()},
    )
    h.capture("sample-documents-replay", {"receipt": replay, "export": replayed})
    business_binding = source(
        h,
        "sample_business",
        version,
        ["/documents/notice"],
        {"extract_business": True},
        robots=True,
    )
    run, _, _ = collect(h, "sample_business", business_binding)
    records = h.cli("export", "sample_business", "--run", run["run_id"])
    h.check("sample_recipe_optional_business_complete", run["status"], "complete")
    notice = next(r for r in records["records"] if r["detail_url"].endswith("/documents/notice"))
    h.check("sample_recipe_business_title", notice["data"]["title"], "Unified public notice")
    h.check("sample_recipe_business_links_saved_version", bool(notice["document_versions"]))
    h.capture("sample-business-records", records)
    before = len(h.site.ledger)
    replay = h.cli("replay", run["run_id"])
    replayed = h.cli("export", "sample_business", "--run", replay["run_id"])
    h.check("sample_recipe_business_replay_zero_network", len(h.site.ledger), before)
    h.check(
        "sample_recipe_business_replay_preserves_values",
        sorted(json.dumps(row["data"], sort_keys=True) for row in replayed["records"]),
        sorted(json.dumps(row["data"], sort_keys=True) for row in records["records"]),
    )
    h.capture("sample-business-replay", {"receipt": replay, "export": replayed})
    original_notice = fixtures["/documents/notice"]
    fixtures["/documents/notice"] = (
        "text/html; charset=utf-8",
        b"<html><article>Public document whose title field is now absent.</article></html>",
    )
    try:
        changed, _, inspected = collect(
            h, "sample_business", business_binding, recheck=True, label="business-unobserved"
        )
        h.check("missing_business_field_still_saves_document", changed["status"], "complete")
        h.check(
            "missing_business_field_marks_known_record_unobserved",
            notice["record_id"]
            in inspected["run"]["report"]["scope_evidence"]["unobserved_records"],
        )
        h.check(
            "unobserved_business_record_is_retained",
            notice["record_id"]
            in {r["record_id"] for r in h.cli("export", "sample_business")["records"]},
        )
    finally:
        fixtures["/documents/notice"] = original_notice
    params = h.root / "invalid-sample-options.json"
    params.write_text(json.dumps({"options": {"body_css": 42}}))
    before = len(h.site.ledger)
    rejected = h.cli(
        "binding", "create", "sample_documents", "--recipe", version, "--params", params, ok=False
    )
    h.check("invalid_parser_config_is_rejected", rejected["error"], "invalid_recipe_params")
    h.check("invalid_parser_config_has_zero_network", len(h.site.ledger), before)
    binding = source(
        h,
        "sample_failed_list",
        version,
        ["/html/list"],
        {"options": {"body_css": "article"}},
        robots=True,
    )
    _, output, _ = collect(h, "sample_failed_list", binding, ok=False)
    h.check(
        "sample_failed_list_still_discovers_two_details",
        {url.rsplit("/", 1)[-1] for url in by_url(output)},
        {"one", "two"},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    h, error = Harness(args.output), None
    try:
        h.start()
        exercise(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("document-pipeline", error)
        path = h.output / "manifest.json"
        value = json.loads(path.read_text())
        value.update(
            version="document.v1",
            command="uv run --frozen python experiments/document-pipeline/acceptance.py --output <new-directory>",
            scope="isolated PostgreSQL and synthetic loopback resources; all formats and zero-network Replay",
        )
        path.write_text(json.dumps(value, indent=2) + "\n")
        summary = {
            "status": "FAIL" if error else "PASS",
            "assertions": len(h.assertions),
            "failed": sum(a["status"] != "PASS" for a in h.assertions),
        }
        h.capture("summary", summary)
        h.close()
    print(json.dumps(summary))
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
