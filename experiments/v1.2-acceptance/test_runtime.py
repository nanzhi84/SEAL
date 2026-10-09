"""Behavioral acceptance: expected business data is independent of Runtime code."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from seal_v12_site import rows

RECIPE = """import os
import scrapy
from seal.core import SealError
from seal.helpers import attachment_record, diagnostic, html_record, is_attachment_response, json_records, static_resources


class AcceptanceSpider(scrapy.Spider):
    name = "heterogeneous_acceptance"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(seed["url"], callback=self.parse,
                                 errback=self.failed, meta={"seal_role":seed["role"]})

    def failed(self, failure):
        yield {"type":"diagnostic", "code":"download_failed"}

    def parse(self, response):
        mode = self.params.get("mode", "api")
        if mode == "api":
            for item in json_records(response):
                if item.get("type") == "record":
                    if self.params.get("projection") == "short" or os.environ.get("SEAL_E2E_RECORD_NONDET"):
                        item["data"].pop("optional")
                        item["locators"].pop("optional")
                    if self.params.get("bad_type"):
                        item["data"]["active"] = int(item["data"]["active"])
                yield item
        elif mode == "assets":
            if response.meta["seal_role"] == "detail":
                if is_attachment_response(response):
                    yield attachment_record(response)
                else:
                    yield html_record(response)
            else:
                yield from static_resources(response, iframe_callback=self.iframe,
                                            attachment_callback=self.attachment,
                                            errback=self.failed)
        elif mode == "parse_failure":
            raise SealError("synthetic_parse_failure")

    def iframe(self, response):
        yield html_record(response)

    def attachment(self, response, parent=None):
        try:
            yield attachment_record(response, parent=parent)
        except SealError as exc:
            yield diagnostic(response, exc.code)
"""


def sql(h, query, params=()):
    with psycopg.connect(h.env["SEAL_DATABASE_URL"], row_factory=dict_row) as connection:
        cursor = connection.execute(query, params)
        return cursor.fetchall() if cursor.description is not None else []


def recipe(h):
    root = h.root / "v12-recipe"
    root.mkdir()
    (root / "recipe.py").write_text(RECIPE)
    (root / "recipe.yaml").write_text(
        json.dumps(
            {
                "family": "v12-synthetic-acceptance",
                "entrypoint": "recipe:AcceptanceSpider",
                "params_schema": {"type": "object"},
            }
        )
    )
    return h.cli("recipe", "pack", root)["recipe_version"]


def record_source(h, name, version, paths=None, params=None, **extra):
    h.config(
        name,
        entries=[h.site.url + path for path in (paths or ["/api"])],
        identity="business_key",
        output_schema="record.v1",
        seed_role="api",
        delay=0.0,
        **extra,
    )
    return h.binding(name, recipe=version, params=params)


def run(h, source, binding, *, recheck=False, ok=True, capture=None):
    args = ["run", source, "--binding", binding]
    if recheck:
        args.append("--recheck")
    receipt = h.cli(*args, ok=ok)
    inspected = h.cli("inspect", "run", receipt["run_id"])
    exported = h.cli("export", source, "--run", receipt["run_id"])
    h.check(source + "_receipt_inspect_status", inspected["run"]["status"], receipt["status"])
    h.check(source + "_receipt_export_status", exported["run"]["status"], receipt["status"])
    h.check(source + "_quality_not_evaluated", exported["quality_status"], "not_evaluated")
    if capture:
        h.capture(capture, {"receipt": receipt, "inspect": inspected, "export": exported})
    return receipt, inspected, exported


def by_key(export):
    return {record["record_key"]: record for record in export["records"]}


def pointer(doc, value):
    for token in value[1:].split("/") if value else []:
        token = token.replace("~1", "/").replace("~0", "~")
        doc = doc[int(token)] if isinstance(doc, list) else doc[token]
    return doc


def archived(h, key):
    path = h.root / "archive/objects" / key[:2] / key[2:]
    data = path.read_bytes()
    h.check("archive_digest_" + key, hashlib.sha256(data).hexdigest(), key)
    return data


def typed_records(h, version):
    binding = record_source(h, "records", version, research_ids=["dd-000"])
    first, inspected, exported = run(h, "records", binding, capture="records-first")
    initial = by_key(exported)
    h.check("ten_typed_records", len(initial), 10)
    h.check(
        "business_data_matches_independent_oracle",
        {key: record["data"] for key, record in initial.items()},
        {row["id"]: row for row in rows()},
    )
    h.check(
        "records_have_no_detail_url",
        all(record["detail_url"] is None for record in initial.values()),
    )
    # Recompute every native JSON field without importing the implementation's locator resolver.
    for result in inspected["record_results"]:
        candidate = result["candidate"]
        snapshot = json.loads(archived(h, result["inputs"][0]))
        raw = json.loads(archived(h, snapshot["body_hash"]))
        for field, expected in candidate["data"].items():
            actual = pointer(raw, candidate["locators"][field]["pointer"])
            h.check(
                "typed_locator_" + candidate["record_key"] + "_" + field,
                type(actual) is type(expected) and actual == expected,
            )
    snapshot = json.loads(archived(h, inspected["record_results"][0]["inputs"][0]))
    key = snapshot["body_hash"]
    path = h.root / "archive/objects" / key[:2] / key[2:]
    original_bytes = path.read_bytes()
    path.write_bytes(b"synthetic evidence corruption")
    try:
        unavailable = h.cli("export", "records", "--run", first["run_id"])
        h.check("corrupt_record_evidence_not_exported", unavailable["records"], [])
        h.check(
            "corrupt_record_evidence_explicit",
            all(item["reason"] == "archive_corrupt" for item in unavailable["unavailable_records"])
            and len(unavailable["unavailable_records"]) == 10,
        )
        h.capture("record-evidence-corruption", unavailable)
    finally:
        path.write_bytes(original_bytes)
    first_versions = {key: row["record_version_id"] for key, row in initial.items()}
    h.site.state = "reorder"
    reordered, _, data = run(h, "records", binding, capture="records-reordered")
    h.check(
        "reorder_no_business_versions",
        {key: row["record_version_id"] for key, row in by_key(data).items()},
        first_versions,
    )
    h.check(
        "reorder_changes_only_raw_evidence",
        all(
            out["raw_changed"] and out["content_change"] == "unchanged"
            for out in reordered["report"]["record_outputs"]
        ),
    )
    h.site.state = "changed"
    changed, _, data = run(h, "records", binding, capture="records-changed")
    current = by_key(data)
    h.check(
        "only_changed_record_new_version",
        [
            key
            for key in sorted(current)
            if current[key]["record_version_id"] != first_versions[key]
        ],
        ["entity-3"],
    )
    h.check(
        "changed_record_keeps_identity",
        current["entity-3"]["record_id"],
        initial["entity-3"]["record_id"],
    )
    h.check(
        "single_source_update_attributed",
        sum(
            out["content_change"] == "source_updated" for out in changed["report"]["record_outputs"]
        ),
        1,
    )
    before = len(h.site.ledger)
    checked, check_inspect, _ = run(h, "records", binding, recheck=True, capture="records-recheck")
    h.check("parent_recheck_one_http_request", len(h.site.ledger) - before, 1)
    h.check("parent_recheck_one_frozen_seed", len(check_inspect["run"]["seeds"]), 1)
    h.check(
        "parent_recheck_preserves_query_and_role", check_inspect["run"]["seeds"][0]["role"], "api"
    )
    h.check("parent_recheck_complete", checked["status"], "complete")
    before = len(h.site.ledger)
    replay = h.cli("replay", first["run_id"])
    replay_inspect = h.cli("inspect", "run", replay["run_id"])
    replay_export = h.cli("export", "records", "--run", replay["run_id"])
    h.check("record_replay_zero_network", len(h.site.ledger), before)
    h.check("record_replay_no_network_observations", replay_inspect["observations"], [])
    h.check(
        "record_replay_original_values",
        {key: row["data"] for key, row in by_key(replay_export).items()},
        {row["id"]: row for row in rows()},
    )
    h.capture(
        "records-replay", {"receipt": replay, "inspect": replay_inspect, "export": replay_export}
    )
    h.site.state = "missing"
    missing_receipt, _, missing = run(h, "records", binding, capture="records-missing")
    h.check("missing_row_not_in_new_run", len(missing["records"]), 9)
    h.check("missing_row_not_deleted_from_history", len(h.export("records")["records"]), 10)
    h.check(
        "missing_row_unknown_explicit",
        missing_receipt["report"]["scope_evidence"]["unobserved_record_count"],
        1,
    )
    h.check(
        "business_population_stays_unknown",
        missing_receipt["report"]["scope_evidence"]["business_population"],
        "unknown",
    )
    # A projection change is located in the same raw bytes, but comes from another Binding.
    short = h.binding("records", recipe=version, params={"projection": "short"})
    reprocessed, _, _ = run(h, "records", short, capture="records-reprocessed")
    h.check(
        "binding_change_is_reprocessing",
        {out["content_change"] for out in reprocessed["report"]["record_outputs"]},
        {"reprocessed"},
    )
    h.site.state = "base"


def duplicates_conflicts_and_types(h, version):
    binding = record_source(h, "duplicates", version, paths=["/api/left", "/api/right"])
    receipt, inspected, exported = run(
        h, "duplicates", binding, capture="same-value-multiple-inputs"
    )
    h.check("same_value_deduplicates_business_records", len(exported["records"]), 10)
    zero = next(
        out
        for out in receipt["report"]["record_outputs"]
        if out["record_id"] == by_key(exported)["entity-0"]["record_id"]
    )
    h.check("same_value_two_inputs_retained", len(zero["inputs"]), 2)
    h.check("same_value_two_result_evidences_retained", len(zero["record_result_ids"]), 2)
    h.check("same_value_emissions_retained", len(inspected["record_emissions"]), 11)
    conflict_binding = record_source(h, "conflict", version)
    _, _, initial = run(h, "conflict", conflict_binding)
    pointers = sql(
        h,
        "SELECT record_key,latest_result,latest_version FROM seal_record WHERE source_id='conflict' ORDER BY record_key",
    )
    h.site.state = "conflict"
    conflict, inspected, exported = run(
        h, "conflict", conflict_binding, ok=False, capture="conflict"
    )
    h.check("same_key_different_value_partial", conflict["status"], "partial")
    h.check("conflict_diagnostic_explicit", "record_identity_conflict" in conflict["errors"])
    h.check("all_conflict_values_excluded", "entity-0" not in by_key(exported))
    h.check("conflict_keeps_all_immutable_emissions", len(inspected["record_emissions"]), 11)
    h.check(
        "partial_does_not_advance_current_pointers",
        sql(
            h,
            "SELECT record_key,latest_result,latest_version FROM seal_record WHERE source_id='conflict' ORDER BY record_key",
        ),
        pointers,
    )
    h.check(
        "partial_does_not_replace_last_success",
        {key: row["data"] for key, row in by_key(h.export("conflict")).items()},
        {key: row["data"] for key, row in by_key(initial).items()},
    )
    h.site.state = "base"
    bad = record_source(h, "badtype", version, params={"bad_type": True})
    failed, _, exported = run(h, "badtype", bad, ok=False, capture="typed-mismatch")
    h.check(
        "boolean_integer_mismatch_rejected", "record_field_locator_mismatch" in failed["errors"]
    )
    h.check("type_mismatch_emits_no_valid_records", exported["records"], [])
    nondeterministic = record_source(h, "nondeterministic", version)
    _, _, baseline = run(h, "nondeterministic", nondeterministic)
    h.env["SEAL_E2E_RECORD_NONDET"] = "1"
    try:
        rejected, _, _ = run(
            h,
            "nondeterministic",
            nondeterministic,
            ok=False,
            capture="nondeterministic-record-output",
        )
        h.check(
            "same_input_binding_nondeterminism_rejected",
            "nondeterministic_record_output" in rejected["errors"],
        )
        h.check(
            "nondeterminism_keeps_prior_success",
            {
                key: row["record_result_id"]
                for key, row in by_key(h.export("nondeterministic")).items()
            },
            {key: row["record_result_id"] for key, row in by_key(baseline).items()},
        )
    finally:
        h.env.pop("SEAL_E2E_RECORD_NONDET")
    empty = record_source(h, "emptyapi", version, paths=["/api?empty=1"])
    empty_receipt, empty_inspect, empty_export = run(h, "emptyapi", empty, capture="empty-api")
    h.check("zero_record_response_technically_complete", empty_receipt["status"], "complete")
    h.check("zero_record_response_retains_raw", len(empty_inspect["observations"]), 1)
    h.check(
        "zero_record_response_has_raw_url_revision",
        bool(empty_inspect["run"]["inputs"][0]["resource_input"]["revision_id"]),
    )
    h.check("zero_record_response_no_fake_result", empty_export["records"], [])
    malformed = record_source(h, "malformedapi", version)
    h.site.state = "malformed"
    try:
        failed, inspected, data = run(
            h, "malformedapi", malformed, ok=False, capture="malformed-row-diagnostic"
        )
        h.check("malformed_row_valid_results_retained", len(data["records"]), 10)
        h.check("malformed_row_diagnostic_attributed", inspected["discovery"]["failed"], 1)
        h.check("malformed_row_reason_explicit", "json_record_parse_failed" in failed["errors"])
    finally:
        h.site.state = "base"


def discovery_and_html(h):
    generic = h.cli("recipe", "pack", "recipes/generic")["recipe_version"]
    h.config("html", entries=[h.site.url + "/html/list"], delay=0.0)
    binding = h.binding("html", recipe=generic)
    receipt, inspected, exported = run(h, "html", binding, capture="html-discovery")
    h.check("html_two_unique_details", len(exported["documents"]), 2)
    h.check(
        "html_duplicate_discoveries_preserved", receipt["report"]["discovery"]["deduplicated"], 2
    )
    events = inspected["discovery"]["events"]
    detail_events = [event for event in events if event["url"].endswith("/html/one")]
    h.check(
        "same_url_discovered_from_both_parents",
        {event["parent_url"] for event in detail_events},
        {h.site.url + "/html/list", h.site.url + "/html/page2"},
    )
    h.check(
        "same_url_uses_one_native_fingerprint",
        len({event["fingerprint"] for event in detail_events}),
        1,
    )
    h.check(
        "detail_downloaded_once", sum(entry["path"] == "/html/one" for entry in h.site.ledger), 1
    )
    h.check(
        "discovery_parent_snapshots_present",
        all(event["parent_snapshot_id"] for event in detail_events),
    )
    before = len(h.site.ledger)
    run(h, "html", binding, recheck=True, capture="html-legacy-recheck")
    h.check(
        "legacy_recheck_only_details",
        {entry["path"] for entry in h.site.ledger[before:]},
        {"/html/one", "/html/two"},
    )
    h.config(
        "legacyparsefailure",
        entries=[h.site.url + "/html/parse-failed"],
        seed_role="detail",
        delay=0.0,
    )
    legacy_failure = h.binding("legacyparsefailure", recipe=generic)
    failed, inspected, data = run(
        h, "legacyparsefailure", legacy_failure, ok=False, capture="legacy-bare-diagnostic"
    )
    h.check(
        "legacy_bare_diagnostic_keeps_original_error",
        "ambiguous_or_missing_field" in failed["errors"],
    )
    h.check("legacy_bare_diagnostic_discovery_attributed", inspected["discovery"]["failed"], 1)
    h.check("legacy_bare_diagnostic_no_fake_document", data["documents"], [])
    for name, path, extra, reason in (
        ("loop", "/html/loop", {}, "pagination_loop"),
        (
            "budget",
            "/html/budget",
            {"budget": {"requests": 3, "seconds": 25, "response_bytes": 100000}},
            "request_budget_exceeded",
        ),
        ("outside", "/html/outside", {"allowed_path_prefixes": ["/html"]}, "request_out_of_scope"),
    ):
        h.config(name, entries=[h.site.url + path], delay=0.0, **extra)
        target = h.binding(name, recipe=generic)
        before = len(h.site.ledger)
        failed, inspected, _ = run(h, name, target, ok=False, capture="discovery-" + name)
        h.check(name + "_partial", failed["status"], "partial")
        h.check(name + "_explicit_reason", reason in failed["errors"])
        if name == "budget":
            h.check("discovery_survives_request_budget", inspected["discovery"]["discovered"], 101)
            h.check("budget_limits_network_requests", len(h.site.ledger) - before <= 3)
            h.check(
                "budget_skips_recorded", inspected["discovery"]["outcomes"].get(reason, 0) >= 97
            )
        if name == "outside":
            h.check(
                "out_of_scope_discovery_preserved",
                inspected["discovery"]["outcomes"].get(reason),
                1,
            )
            h.check(
                "out_of_scope_not_requested",
                not any(entry["path"] == "/denied/one" for entry in h.site.ledger[before:]),
            )


def assets_and_manifest(h, version):
    binding = record_source(h, "assets", version, paths=["/assets/list"], params={"mode": "assets"})
    receipt, inspected, exported = run(h, "assets", binding, capture="static-resources")
    h.check("iframe_pdf_csv_three_records", len(exported["records"]), 3)
    h.check(
        "attachment_parent_input_preserved",
        sorted(len(out["inputs"]) for out in receipt["report"]["record_outputs"]),
        [1, 2, 2],
    )
    h.check(
        "synthetic_pdf_archived_exact_bytes",
        any(
            json.loads(archived(h, observation["snapshot_id"]))["body_hash"]
            == hashlib.sha256(h.site.pdf).hexdigest()
            for observation in inspected["observations"]
            if observation["url"].endswith("text.pdf")
        ),
    )
    before = len(h.site.ledger)
    replay = h.cli("replay", receipt["run_id"])
    h.check("attachment_replay_zero_network", len(h.site.ledger), before)
    h.check("attachment_replay_complete", replay["status"], "complete")
    first_versions = {key: row["record_version_id"] for key, row in by_key(exported).items()}
    before = len(h.site.ledger)
    _, _, checked = run(h, "assets", binding, recheck=True, capture="attachment-recheck")
    h.check(
        "attachment_recheck_three_details_only",
        {row["path"] for row in h.site.ledger[before:]},
        {"/assets/frame", "/assets/text.pdf", "/assets/data.csv"},
    )
    h.check(
        "attachment_recheck_content_versions_stable",
        {key: row["record_version_id"] for key, row in by_key(checked).items()},
        first_versions,
    )
    bad = record_source(
        h, "badassets", version, paths=["/assets/bad-list"], params={"mode": "assets"}
    )
    failed, inspected_bad, data = run(
        h, "badassets", bad, ok=False, capture="unsupported-resources"
    )
    h.check("unparseable_attachments_partial", failed["status"], "partial")
    h.check("unparseable_attachments_no_fabricated_record", data["records"], [])
    h.check("unparseable_attachment_bytes_archived", len(inspected_bad["observations"]), 3)
    h.check("unparseable_attachment_discovery_attributed", inspected_bad["discovery"]["failed"], 2)
    manifest = h.cli("inspect", "manifest", receipt["report"]["manifest_id"])["manifest"]
    h.check("manifest_exact_run", manifest["run_id"], receipt["run_id"])
    h.check("manifest_exact_binding", manifest["binding_id"], binding)
    h.check("manifest_status_consistent", manifest["run_status"], receipt["status"])
    h.check("manifest_quality_not_evaluated", manifest["quality_status"], "not_evaluated")
    h.check("manifest_record_mappings", len(manifest["artifacts"]["records"]), 3)
    h.capture("runtime-manifest", manifest)
    research = h.cli("inspect", "research", "dd-000")
    h.check(
        "research_mapping_frozen_binding",
        bool(research["runs"])
        and all(row["source_id"] == "records" and row["manifest_id"] for row in research["runs"]),
    )


def failures_and_manifest_fencing(h, version):
    parse = record_source(
        h, "parsefailure", version, paths=["/html/parse-failed"], params={"mode": "parse_failure"}
    )
    failed, inspected, _ = run(h, "parsefailure", parse, ok=False, capture="parse-failure")
    h.check("parse_error_causes_partial", failed["status"], "partial")
    h.check("parse_error_discovery_attributed", inspected["discovery"]["failed"], 1)
    h.check("parse_error_raw_preserved", len(inspected["observations"]), 1)
    h.config(
        "transportfailure",
        entries=[h.site.url + "/html/download-failed"],
        seed_role="detail",
        delay=0.0,
    )
    transport = h.binding("transportfailure")
    _, inspected, _ = run(h, "transportfailure", transport, ok=False, capture="transport-failure")
    h.check("download_error_discovery_attributed", inspected["discovery"]["failed"] >= 1)
    h.check(
        "download_error_never_fabricates_snapshot",
        all(observation["snapshot_id"] is None for observation in inspected["observations"]),
    )
    binding = record_source(h, "manifestfault", version)
    pending = h.cli("run", "manifestfault", "--binding", binding, "--enqueue")
    script = """import json
from seal import completion
from seal.runs import execute_run
def fail_manifest(*args, **kwargs):
    raise OSError("synthetic manifest write fault")
completion.write_manifest = fail_manifest
print(json.dumps(execute_run(%r)))
""" % pending["run_id"]
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=h.env,
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    failed = json.loads(result.stdout)
    h.check("manifest_write_failure_partial", failed["status"], "partial")
    h.check("manifest_write_failure_diagnostic", "manifest_archive_failed" in failed["errors"])
    h.check(
        "manifest_failure_no_current_pointer",
        all(
            record["latest_result"] is None
            for record in sql(
                h, "SELECT latest_result FROM seal_record WHERE source_id='manifestfault'"
            )
        ),
    )
    h.check(
        "manifest_failure_keeps_valid_emissions",
        len(h.cli("inspect", "run", failed["run_id"])["record_emissions"]),
        10,
    )
    h.capture("manifest-write-fault", failed)


def worker_fencing_and_migration(h, version):
    binding = record_source(h, "worker", version)
    queued = h.cli("run", "worker", "--binding", binding, "--enqueue")
    h.start_worker()
    h.wait_worker()
    worker = h.cli("inspect", "run", queued["run_id"])
    h.check("record_worker_complete", worker["run"]["status"], "complete")
    h.check("record_worker_ten_results", len(worker["run"]["report"]["record_outputs"]), 10)
    h.capture("record-worker", worker)
    h.select("worker", binding, 0)
    sql(h, "UPDATE seal_source SET next_poll=now()+interval '1 day' WHERE id='worker'")
    sql(h, "UPDATE seal_record SET next_check=now()-interval '1 second' WHERE source_id='worker'")
    empty_binding = sql(h, "SELECT id FROM seal_binding WHERE source_id='emptyapi'")[0]["id"]
    h.select("emptyapi", empty_binding, 0)
    sql(h, "UPDATE seal_source SET next_poll=now()+interval '1 day' WHERE id='emptyapi'")
    sql(
        h,
        "UPDATE seal_document SET next_check=now()-interval '1 second' WHERE source_id='emptyapi'",
    )
    before = len(h.site.ledger)
    scheduled = h.cli("schedule")
    h.check("record_due_scheduler_queues_once", len(scheduled["run_ids"]), 1)
    scheduled_run = h.cli("inspect", "run", scheduled["run_ids"][0])
    h.check("record_due_scheduler_recheck_mode", scheduled_run["run"]["mode"], "recheck")
    h.check("record_due_scheduler_deduplicates_parent", len(scheduled_run["run"]["seeds"]), 1)
    h.start_worker()
    h.wait_worker()
    h.check("record_due_worker_one_parent_request", len(h.site.ledger) - before, 1)
    h.check(
        "record_due_worker_complete",
        h.cli("inspect", "run", scheduled["run_ids"][0])["run"]["status"],
        "complete",
    )
    h.check("record_due_scheduler_not_double_queued", h.cli("schedule")["run_ids"], [])
    frozen_before = h.cli("inspect", "run", queued["run_id"])["run"]
    source_before = h.cli("inspect", "source", "worker")["source"]
    script = """import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from scrapy import Request
from seal.archive import InputMiddleware, ResponseArchiveMiddleware, restore
from seal.crawl import Completion
from seal.runs import run_context
context = run_context(%r, 1)
entry = context["inputs"][0]
request = Request(entry["logical_url"], meta={
    "seal_snapshot_id":entry["snapshot_id"], "seal_observation_id":entry["observation_id"],
    "seal_role":entry["role"], "seal_request_key":entry["key"],
    "seal_logical_url":entry["logical_url"], "seal_original_fetched_at":entry["fetched_at"]})
middleware = InputMiddleware()
middleware.context, middleware.config = context, context["config"]
try:
    middleware.process_spider_input(restore(entry["snapshot_id"], request))
except Exception:
    pass
archive = ResponseArchiveMiddleware()
archive.context = context
try:
    archive.cooldown(datetime.now(timezone.utc)+timedelta(days=1))
except Exception:
    pass
completion = Completion()
completion.context = context
completion.crawler = SimpleNamespace(
    stats=SimpleNamespace(get_stats=lambda:{"seal/errors":1}),
    engine=SimpleNamespace(downloader=SimpleNamespace(middleware=SimpleNamespace(middlewares=[]))))
asyncio.run(completion.closed(object(), "finished"))
print(json.dumps({"late_callbacks_executed":True}))
""" % queued["run_id"]
    subprocess.run(
        [sys.executable, "-c", script],
        env=h.env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    frozen_after = h.cli("inspect", "run", queued["run_id"])["run"]
    h.check(
        "late_callback_cannot_mutate_frozen_inputs", frozen_after["inputs"], frozen_before["inputs"]
    )
    h.check(
        "late_close_cannot_mutate_terminal_errors", frozen_after["errors"], frozen_before["errors"]
    )
    h.check(
        "late_close_cannot_mutate_terminal_report", frozen_after["report"], frozen_before["report"]
    )
    h.check(
        "old_cooldown_cannot_mutate_source",
        h.cli("inspect", "source", "worker")["source"]["cooldown_until"],
        source_before["cooldown_until"],
    )
    h.capture("late-callback-fencing", {"before": frozen_before, "after": frozen_after})
    fence = record_source(h, "fence", version, paths=["/api/fence"])
    h.site.hold_next = True
    process = subprocess.Popen(
        [sys.executable, "-m", "seal", "run", "fence", "--binding", fence],
        env=h.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        h.check("old_record_callback_in_flight", h.site.hold_started.wait(timeout=10))
        h.site.state = "changed"
        newest, _, exported = run(h, "fence", fence)
        h.site.hold_release.set()
        stdout, stderr = process.communicate(timeout=30)
        old = json.loads(stdout)
        h.check("obsolete_record_run_superseded", old["status"], "superseded")
        h.check(
            "obsolete_callback_cannot_overwrite",
            by_key(h.export("fence"))["entity-3"]["data"]["count"],
            33,
        )
        h.capture(
            "record-fencing",
            {"old": old, "new": newest, "export": exported, "stderr": stderr[-1000:]},
        )
    finally:
        h.site.hold_release.set()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        h.site.state = "base"
    migrations = sql(h, "SELECT name,checksum FROM seal_migration ORDER BY name")
    h.cli("db", "migrate")
    h.check(
        "incremental_migration_idempotent",
        sql(h, "SELECT name,checksum FROM seal_migration ORDER BY name"),
        migrations,
    )
    for row in migrations:
        path = (
            Path("src/seal/schema.sql")
            if row["name"] == "schema.sql"
            else Path("src/seal/migrations") / row["name"]
        )
        h.check(
            "applied_migration_digest_" + row["name"],
            hashlib.sha256(path.read_bytes()).hexdigest(),
            row["checksum"],
        )
    # Immutable proof must actually attempt mutation, in a rollback-only transaction.
    for table in ("seal_record_result", "seal_record_version", "seal_record_emission"):
        rejected = False
        with psycopg.connect(h.env["SEAL_DATABASE_URL"]) as connection:
            try:
                connection.execute(f"DELETE FROM {table}")
            except psycopg.Error as exc:
                rejected = "immutable SEAL record" in str(exc)
            connection.rollback()
        h.check(table + "_immutable", rejected)
    h.capture("migration-checksums", migrations)


def run_all(h):
    version = recipe(h)
    typed_records(h, version)
    duplicates_conflicts_and_types(h, version)
    discovery_and_html(h)
    assets_and_manifest(h, version)
    failures_and_manifest_fencing(h, version)
    worker_fencing_and_migration(h, version)
