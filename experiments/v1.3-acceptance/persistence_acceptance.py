"""Manual Collect contracts through fresh CLI processes, real PostgreSQL and archive.

This isolated suite verifies mechanics. Retained real-source collections are a
separate acceptance requirement and must not reuse this temporary cluster.
"""

import argparse
import hashlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
V12 = ROOT / "experiments/v1.2-acceptance"
sys.path.insert(0, str(V12))
SPEC = importlib.util.spec_from_file_location("seal_v12_acceptance", V12 / "acceptance.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
Harness = MODULE.Harness

from runtime_support import by_key, recipe, record_source, sql  # noqa: E402


def run_all(h):
    version = recipe(h)
    binding = record_source(h, "persistent", version)
    h.select("persistent", binding, 0)
    first = h.cli("collect", "persistent")
    inspected = h.cli("inspect", "run", first["run_id"])
    initial = by_key(h.cli("export", "persistent", "--run", first["run_id"]))
    h.check("manual_collect_uses_default_binding", first["binding_id"], binding)
    h.check("manual_collect_fresh_query_summary", inspected["summary"], first["summary"])
    h.check(
        "manual_collect_terminal_reason", first["summary"]["terminal_reason"], "queue_exhausted"
    )
    h.check("manual_collect_record_count", first["summary"]["counts"]["records"], 10)
    h.check("manual_collect_snapshot_count", first["summary"]["counts"]["snapshots"], 1)
    h.check("manual_collect_archive_count", first["summary"]["counts"]["archived"], 1)
    h.check(
        "manual_collect_unknown_business_population",
        first["report"]["scope_evidence"]["business_population"],
        "unknown",
    )
    record_id = initial["entity-3"]["record_id"]
    first_version = initial["entity-3"]["record_version_id"]
    record = h.cli("inspect", "record", record_id)
    h.check(
        "record_fresh_process_query_current_pointer",
        record["record"]["latest_version"],
        first_version,
    )
    h.check("record_query_first_version", len(record["versions"]), 1)
    snapshot_id = inspected["observations"][0]["snapshot_id"]
    snapshot = h.cli("inspect", "snapshot", snapshot_id)
    h.check(
        "snapshot_fresh_query_archive_available", snapshot["archive"]["availability"], "available"
    )
    h.check("snapshot_fresh_query_observation_count", len(snapshot["observations"]), 1)
    h.check("snapshot_api_returns_references_not_body", "body" in snapshot, False)

    repeated = h.cli("run", "persistent", "--binding", binding)
    repeat_export = by_key(h.cli("export", "persistent", "--run", repeated["run_id"]))
    h.check("legacy_run_still_collects", repeated["mode"], "collect")
    h.check(
        "repeat_collect_same_record_identities",
        {key: value["record_id"] for key, value in repeat_export.items()},
        {key: value["record_id"] for key, value in initial.items()},
    )
    h.check(
        "repeat_collect_same_business_versions",
        {key: value["record_version_id"] for key, value in repeat_export.items()},
        {key: value["record_version_id"] for key, value in initial.items()},
    )
    snapshot = h.cli("inspect", "snapshot", snapshot_id)
    h.check("repeat_collect_reuses_snapshot_keeps_observations", len(snapshot["observations"]), 2)
    h.check(
        "repeat_collect_no_duplicate_business_rows",
        sql(h, "SELECT count(*) AS n FROM seal_record WHERE source_id='persistent'")[0]["n"],
        10,
    )

    h.site.state = "reorder"
    reordered = h.cli("collect", "persistent")
    record = h.cli("inspect", "record", record_id)
    h.check("raw_reordering_keeps_business_version", len(record["versions"]), 1)
    h.check(
        "raw_reordering_still_tracked",
        all(
            output["raw_changed"] and output["content_change"] == "unchanged"
            for output in reordered["report"]["record_outputs"]
        ),
    )
    h.site.state = "changed"
    changed = h.cli("collect", "persistent")
    changed_record = h.cli("inspect", "record", record_id)
    h.check("changed_content_retains_identity", changed_record["record"]["id"], record_id)
    h.check("changed_content_adds_business_version", len(changed_record["versions"]), 2)
    h.check(
        "changed_content_predecessor", changed_record["versions"][-1]["predecessor"], first_version
    )
    h.site.state = "base"
    reverted = h.cli("collect", "persistent")
    record = h.cli("inspect", "record", record_id)
    h.check("content_reversion_appends_third_version", len(record["versions"]), 3)
    h.check(
        "content_reversion_preserves_chain",
        record["versions"][-1]["predecessor"],
        record["versions"][-2]["id"],
    )
    h.check(
        "content_reversion_matches_initial_hash",
        record["versions"][-1]["content_hash"],
        record["versions"][0]["content_hash"],
    )
    h.check(
        "queries_do_not_rewrite_frozen_report",
        h.cli("inspect", "run", first["run_id"])["run"]["report"],
        first["report"],
    )
    before_replay = len(h.site.ledger)
    replayed = h.cli("replay", first["run_id"])
    h.check(
        "replay_summary_has_no_http_attempts", replayed["summary"]["counts"]["http_attempts"], 0
    )
    h.check("replay_summary_keeps_input_snapshots", replayed["summary"]["counts"]["snapshots"], 1)
    h.check("replay_summary_keeps_emitted_records", replayed["summary"]["counts"]["records"], 10)
    h.check("replay_summary_query_does_not_download", len(h.site.ledger), before_replay)
    h.check(
        "replay_keeps_online_current_version",
        h.cli("inspect", "record", record_id)["record"]["latest_version"],
        record["record"]["latest_version"],
    )

    # A later conflict retains all immutable evidence without promoting bad current data.
    baseline_pointer = record["record"]["latest_version"]
    h.site.state = "conflict"
    conflict = h.cli("collect", "persistent", ok=False)
    h.check("conflicting_collect_partial", conflict["status"], "partial")
    h.check(
        "partial_collect_preserves_current_version",
        h.cli("inspect", "record", record_id)["record"]["latest_version"],
        baseline_pointer,
    )
    h.site.state = "base"

    # Queue insertion and a source Run commit together, with state available immediately.
    queued = h.cli("collect", "persistent", "--enqueue")
    h.check("queued_collect_pending", queued["status"], "pending")
    h.check("queued_collect_not_terminal", queued["summary"]["terminal"], False)
    h.check(
        "queued_collect_has_persisted_job",
        h.cli("inspect", "run", queued["run_id"])["run"]["job_id"] is not None,
    )
    h.start_worker()
    h.wait_worker()
    completed = h.cli("inspect", "run", queued["run_id"])
    h.check("queued_collect_worker_completes", completed["run"]["status"], "complete")

    # Original private bytes remain queryable; corruption is explicit and cannot fake success.
    snapshot = h.cli("inspect", "snapshot", snapshot_id)
    body_hash = snapshot["snapshot"]["body_hash"]
    path = h.root / "archive/objects" / body_hash[:2] / body_hash[2:]
    original = path.read_bytes()
    path.write_bytes(b"Synthetic corrupted archive")
    try:
        broken = h.cli("inspect", "snapshot", snapshot_id)
        h.check(
            "corrupt_snapshot_query_is_explicit",
            broken["archive"],
            {"availability": "unavailable", "reason": "archive_corrupt"},
        )
    finally:
        path.write_bytes(original)
    missing = h.cli("inspect", "record", "missing-record", ok=False)
    h.check("missing_record_query_is_explicit", missing["error"], "record_not_found")
    h.check(
        "non_snapshot_object_rejected",
        h.cli("inspect", "snapshot", version, ok=False)["error"],
        "unsupported_snapshot_contract",
    )
    asset_binding = record_source(
        h, "persist_assets", version, paths=["/assets/list"], params={"mode": "assets"}
    )
    assets = h.cli("collect", "persist_assets", "--binding", asset_binding)
    h.check(
        "attachment_summary_counts_archived_snapshots",
        assets["summary"]["counts"]["attachments"],
        2,
    )
    h.check(
        "attachment_summary_counts_all_input_snapshots", assets["summary"]["counts"]["snapshots"], 4
    )
    h.check(
        "attachment_summary_counts_structured_records", assets["summary"]["counts"]["records"], 3
    )

    # Fresh subprocesses cannot reset a Run's finite attempt limit or original deadline.
    failed_binding = record_source(h, "persist_retry", version, paths=["/html/download-failed"])
    failure = h.cli("collect", "persist_retry", "--binding", failed_binding, ok=False)
    initial_deadline = failure["deadline"]
    h.check("transient_collect_retryable", failure["status"], "retryable")
    for epoch in (2, 3):
        failure = h.cli("retry", failure["run_id"], ok=False)
        h.check(f"retry_attempt_{epoch}_bounded_epoch", failure["attempt_epoch"], epoch)
        h.check(f"retry_attempt_{epoch}_same_deadline", failure["deadline"], initial_deadline)
    h.check("retry_limit_ends_failed", failure["status"], "failed")
    h.check(
        "retry_after_limit_rejected",
        h.cli("retry", failure["run_id"], ok=False)["error"],
        "run_not_retryable",
    )
    exhausted = h.cli("inspect", "run", failure["run_id"])
    h.check("retry_history_retained", len(exhausted["run"]["report"]["attempts"]), 2)
    h.check(
        "retry_attempt_counts_exclude_prior_epochs", exhausted["summary"]["counts"]["requested"], 3
    )
    h.capture(
        "manual-persistence",
        {
            "first": first,
            "repeated": repeated,
            "changed": changed,
            "reverted": reverted,
            "record": record,
            "snapshot": snapshot,
            "queued": completed,
            "exhausted": exhausted,
        },
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    h = Harness(args.output)
    error = None
    try:
        h.start()
        run_all(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("v1.3-persistence", error)
        path = h.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(
            version="v1.3",
            command="uv run --frozen python experiments/v1.3-acceptance/persistence_acceptance.py --output <new-directory>",
            scope="Manual Collect engineering contracts: fresh CLI processes, isolated PostgreSQL and loopback HTTP",
        )
        manifest["code"].update(
            {
                str(file.relative_to(ROOT)): hashlib.sha256(file.read_bytes()).hexdigest()
                for file in Path(__file__).parent.glob("*.py")
            }
        )
        path.write_text(json.dumps(manifest, indent=2))
        h.close()
    print(
        json.dumps(
            {
                "status": "FAIL" if error else "PASS",
                "assertions": len(h.assertions),
                "output": str(args.output),
            }
        )
    )
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
