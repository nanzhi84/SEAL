"""Record, Discovery and attachment acceptance through real Runtime paths."""

import hashlib
import json
import subprocess
import sys

from runtime_support import archived, by_key, pointer, recipe, record_source, run, sql
from runtime_worker import worker_fencing_and_migration
from seal_v12_site import rows


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


def run_all(h):
    version = recipe(h)
    from sensitive_acceptance import sensitive_responses

    sensitive_responses(h, version)
    from runtime_attachment import structured_attachments

    structured_attachments(h, version)
    typed_records(h, version)
    duplicates_conflicts_and_types(h, version)
    discovery_and_html(h)
    assets_and_manifest(h, version)
    failures_and_manifest_fencing(h, version)
    worker_fencing_and_migration(h, version)
    from recheck_acceptance import run_all as planned_recheck

    planned_recheck(h)
