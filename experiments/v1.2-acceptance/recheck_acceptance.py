"""Failure-first CLI/Worker/PG contracts for identity and bounded planned Recheck.

Identifiable failures: detail URLs replacing business keys; premature next_check;
all-history absence counts; unbounded due seeds; duplicate parent requests;
failed targets starving other due targets; stale generations changing retry state;
unprovable historical parents guessed from current Source entries.
"""

import argparse
import json
import sys
import traceback
from pathlib import Path

from acceptance import Harness
from recheck_site import Site
from runtime_support import archived, by_key, pointer, record_source, run, sql

RECIPE = """import scrapy
from seal.helpers import html_record, json_records

class RecheckSpider(scrapy.Spider):
    name = "planned_recheck_acceptance"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.context, self.params = context, params

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(seed["url"], callback=self.parse,
                                 meta={"seal_role": seed["role"]})

    def parse(self, response):
        if response.meta["seal_role"] == "api":
            for item in json_records(response, detail_url_field="detail_url"):
                if self.params.get("legacy") and item.get("type") == "record":
                    item["frozen_parent_request"] = None
                yield item
        else:
            yield html_record(response)
"""


def pack(h):
    root = h.root / "recheck-recipe"
    root.mkdir()
    (root / "recipe.py").write_text(RECIPE)
    (root / "recipe.yaml").write_text(
        json.dumps(
            {
                "family": "planned-recheck-acceptance",
                "entrypoint": "recipe:RecheckSpider",
                "params_schema": {"type": "object"},
            }
        )
    )
    return h.cli("recipe", "pack", root)["recipe_version"]


def dates(h, source):
    return sql(
        h,
        "SELECT id,record_key,next_check FROM seal_record WHERE source_id=%s "
        "AND namespace='runtime' ORDER BY record_key",
        (source,),
    )


def scheduled(h):
    result = h.cli("schedule")
    h.check("single_source_due_run", len(result["run_ids"]), 1)
    return h.cli("inspect", "run", result["run_ids"][0])["run"]


def completed(h, planned):
    h.start_worker()
    h.wait_worker()
    result = h.cli("inspect", "run", planned["id"])["run"]
    h.check("planned_worker_complete", result["status"], "complete")
    h.capture("planned-" + planned["id"], result)
    return result


def activate(h, source, binding):
    current = h.cli("inspect", "source", source)["source"]
    h.select(source, binding, current["generation"])
    sql(h, "UPDATE seal_source SET next_poll=now()+interval '1 day' WHERE id=%s", (source,))


def identity(h, version):
    binding = record_source(h, "jsondetail", version, paths=["/api/group-0"])
    first, _, export = run(h, "jsondetail", binding, capture="json-detail-first")
    h.check("json_first_keys", sorted(by_key(export)), ["123", "124"])
    identities = {record["record_key"]: record["record_id"] for record in export["records"]}
    h.site.reverse = True
    recollected, _, reordered = run(h, "jsondetail", binding, capture="json-detail-reordered")
    h.check(
        "json_reordered_collect_same_ids",
        {record["record_key"]: record["record_id"] for record in reordered["records"]},
        identities,
    )
    before = len(h.site.ledger)
    second, _, export = run(h, "jsondetail", binding, recheck=True, capture="json-detail-recheck")
    h.check("json_detail_recheck_complete", second["status"], "complete")
    h.check("json_detail_recheck_stable_keys", sorted(by_key(export)), ["123", "124"])
    h.check(
        "json_detail_recheck_same_ids",
        {record["record_key"]: record["record_id"] for record in export["records"]},
        identities,
    )
    h.check(
        "json_detail_recheck_parent_get",
        h.site.ledger[before:],
        [
            {"method": "GET", "path": "/api/group-0", "status": 200},
        ],
    )
    h.check("json_detail_no_new_identities", len(dates(h, "jsondetail")), 2)
    for record in export["records"]:
        candidate = record
        raw = json.loads(archived(h, candidate["primary_snapshot_id"]))
        body = json.loads(archived(h, raw["body_hash"]))
        h.check(
            "json_detail_original_body",
            pointer(body, candidate["locators"]["body"]["pointer"]),
            "Original JSON business content",
        )
    count = len(h.site.ledger)
    replay = h.cli("replay", second["run_id"])
    h.check("json_detail_replay_complete", replay["status"], "complete")
    h.check("json_detail_replay_zero_network", len(h.site.ledger), count)
    # Public historical rows with parent metadata missing recover only from accepted inputs.
    sql(h, "UPDATE seal_record SET parent_request=NULL WHERE source_id='jsondetail'")
    recovered, _, _ = run(h, "jsondetail", binding, recheck=True)
    h.check("historical_parent_proven_from_input", recovered["status"], "complete")
    h.capture(
        "identity-runs",
        {"first": first, "recollected": recollected, "second": second, "replay": replay},
    )
    # Simulate a prior valid JSON+detail candidate, without rewriting immutable evidence.
    old_binding = record_source(
        h, "historicaldetail", version, paths=["/api/group-0"], params={"legacy": True}
    )
    old, _, _ = run(h, "historicaldetail", old_binding)
    h.check(
        "legacy_candidate_really_lacks_parent",
        all(
            row["candidate"]["frozen_parent_request"] is None
            for row in sql(
                h,
                "SELECT rr.candidate FROM seal_record_result rr JOIN seal_record r "
                "ON r.latest_result=rr.id WHERE r.source_id='historicaldetail'",
            )
        ),
    )
    new_binding = h.binding("historicaldetail", recipe=version)
    recovered, _, export = run(h, "historicaldetail", new_binding, recheck=True)
    h.check("legacy_candidate_parent_recovery", sorted(by_key(export)), ["123", "124"])
    h.check("legacy_candidate_parent_complete", recovered["status"], "complete")
    missing_binding = record_source(
        h, "unprovableparent", version, paths=["/api/group-0"], params={"legacy": True}
    )
    missing, _, _ = run(h, "unprovableparent", missing_binding)
    sql(h, "UPDATE seal_run SET inputs='[]' WHERE id=%s", (missing["run_id"],))
    before = dates(h, "unprovableparent")
    rejected = h.cli("run", "unprovableparent", "--binding", missing_binding, "--recheck", ok=False)
    h.check(
        "unproven_parent_never_detail_fallback", rejected["error"], "record_recheck_request_missing"
    )
    h.check("unproven_parent_no_date_change", dates(h, "unprovableparent"), before)
    h.capture("historical-parent-cases", {"accepted_old": old, "unprovable_old": missing})
    activate(h, "unprovableparent", missing_binding)
    sql(
        h,
        "UPDATE seal_record SET next_check=now()-interval '1 hour' WHERE source_id='unprovableparent'",
    )
    valid_binding = record_source(h, "goodparent", version, paths=["/api/group-0"])
    run(h, "goodparent", valid_binding)
    activate(h, "goodparent", valid_binding)
    sql(h, "UPDATE seal_record SET next_check=now()-interval '1 hour' WHERE source_id='goodparent'")
    before = dates(h, "unprovableparent")
    result = h.cli("schedule")
    h.check("rejected_source_does_not_block_valid_source", len(result["run_ids"]), 1)
    h.check(
        "schedule_bad_parent_explicit_reason",
        result["recheck_rejections"],
        [
            {
                "source_id": "unprovableparent",
                "mode": "recheck",
                "reason": "record_recheck_request_missing",
            },
        ],
    )
    h.check("schedule_rejected_source_dates_unchanged", dates(h, "unprovableparent"), before)
    completed(h, h.cli("inspect", "run", result["run_ids"][0])["run"])
    for source in ("unprovableparent", "goodparent"):
        h.cli("pause", source, "--reason", "fixture finished")
    # A Source can retain generic-document history after selecting Record output.
    h.config("schema-transition", entries=[h.site.url + "/api/group-0"], seed_role="api", delay=0.0)
    generic = h.binding("schema-transition")
    initial = h.cli("run", "schema-transition", "--binding", generic)
    h.check("old_generic_api_document_complete", initial["status"], "complete")
    record_binding = record_source(h, "schema-transition", version, paths=["/api/group-0"])
    run(h, "schema-transition", record_binding)
    activate(h, "schema-transition", record_binding)
    sql(
        h,
        "UPDATE seal_document SET next_check=now()-interval '1 hour' WHERE source_id='schema-transition'",
    )
    h.check("binding_schema_ignores_old_generic_due", h.cli("schedule")["run_ids"], [])
    sql(
        h,
        "UPDATE seal_record SET next_check=now()-interval '1 hour' WHERE source_id='schema-transition'",
    )
    planned = scheduled(h)
    h.check("record_schema_excludes_generic_targets", planned["recheck_plan"]["documents"], [])
    h.check(
        "record_schema_preserves_parent_api_role",
        planned["seeds"],
        [
            {"url": h.site.url + "/api/group-0", "role": "api", "method": "GET"},
        ],
    )
    old_dates = sql(
        h, "SELECT id,next_check FROM seal_document WHERE source_id='schema-transition' ORDER BY id"
    )
    completed(h, planned)
    h.check(
        "raw_resource_does_not_postpone_generic_history",
        sql(
            h,
            "SELECT id,next_check FROM seal_document "
            "WHERE source_id='schema-transition' ORDER BY id",
        ),
        old_dates,
    )
    h.check("generic_history_retained", len(h.export("schema-transition")["documents"]), 1)
    h.cli("pause", "schema-transition", "--reason", "fixture finished")


def batch(h, version):
    name = "urlbatch"
    paths = [f"/documents/{i:03d}" for i in range(200)]
    h.config(
        name,
        entries=[h.site.url + path for path in paths],
        identity="business_key",
        output_schema="record.v1",
        seed_role="detail",
        delay=0.0,
        budget={"requests": 250, "seconds": 60, "response_bytes": 100000},
    )
    binding = h.binding(name, recipe=version)
    receipt, _, _ = run(h, name, binding)
    h.check("url_batch_initial_200", receipt["report"]["record_counts"]["record_count"], 200)
    h.config(
        name,
        entries=[h.site.url + path for path in paths],
        identity="business_key",
        output_schema="record.v1",
        seed_role="detail",
        delay=0.0,
        budget={"requests": 100, "seconds": 60, "response_bytes": 100000},
    )
    binding = h.binding(name, recipe=version)
    activate(h, name, binding)
    sql(h, "UPDATE seal_record SET next_check=now()+interval '1 day' WHERE source_id=%s", (name,))
    sql(
        h,
        "UPDATE seal_record SET next_check=now()-interval '1 hour' WHERE source_id=%s "
        "AND record_key=%s",
        (name, h.site.url + "/documents/000"),
    )
    before = dates(h, name)
    planned = scheduled(h)
    h.check("queue_does_not_postpone_any_record", dates(h, name), before)
    h.check("one_due_one_seed", len(planned["seeds"]), 1)
    h.check("same_due_not_double_queued", h.cli("schedule")["run_ids"], [])
    result = completed(h, planned)
    h.check(
        "one_due_not_199_unobserved",
        result["report"]["scope_evidence"]["unobserved_record_count"],
        0,
    )
    sql(
        h,
        "UPDATE seal_record SET next_check=now()-interval '1 hour' WHERE source_id=%s "
        "AND record_key<=%s",
        (name, h.site.url + "/documents/100"),
    )
    before = dates(h, name)
    first = scheduled(h)
    h.check("101_due_first_batch_100", len(first["seeds"]), 100)
    h.check("101_due_queue_dates_unchanged", dates(h, name), before)
    completed(h, first)
    second = scheduled(h)
    h.check("101_due_second_batch_one", len(second["seeds"]), 1)
    h.check(
        "batches_do_not_repeat_target",
        not ({x["url"] for x in first["seeds"]} & {x["url"] for x in second["seeds"]}),
    )
    completed(h, second)
    h.check("all_due_batches_drained", h.cli("schedule")["run_ids"], [])
    before, runs = dates(h, name), sql(h, "SELECT id FROM seal_run WHERE source_id=%s", (name,))
    rejected = h.cli("run", name, "--binding", binding, "--recheck", ok=False)
    h.check("manual_all_over_budget_explicit", rejected["error"], "recheck_budget_exceeded")
    h.check(
        "manual_over_budget_no_new_run",
        sql(h, "SELECT id FROM seal_run WHERE source_id=%s", (name,)),
        runs,
    )
    h.check("manual_over_budget_no_date_change", dates(h, name), before)
    h.cli("pause", name, "--reason", "fixture finished")


def failure(h, version):
    name = "retryfair"
    binding = record_source(h, name, version, paths=["/api/group-0", "/api/group-1"])
    run(h, name, binding)
    h.config(
        name,
        entries=[h.site.url + "/api/group-0", h.site.url + "/api/group-1"],
        identity="business_key",
        output_schema="record.v1",
        seed_role="api",
        delay=0.0,
        budget={"requests": 1, "seconds": 25, "response_bytes": 100000},
    )
    binding = h.binding(name, recipe=version)
    activate(h, name, binding)
    sql(h, "UPDATE seal_record SET next_check=now()-interval '1 hour' WHERE source_id=%s", (name,))
    before = dates(h, name)
    first = scheduled(h)
    h.check("shared_parent_dedup_budget", len(first["seeds"]), 1)
    h.check("shared_parent_two_targets", len(first["recheck_plan"]["records"]), 2)
    failed_path = first["seeds"][0]["url"].replace(h.site.url, "")
    h.site.failed.add(failed_path)
    sql(h, "UPDATE seal_run SET max_attempts=1 WHERE id=%s", (first["id"],))
    h.start_worker()
    h.wait_worker()
    failed = h.cli("inspect", "run", first["id"])["run"]
    h.check("failed_due_run_visible", failed["status"] in ("failed", "partial"))
    h.check("failed_due_run_reports_http_error", "http_5xx" in failed["errors"])
    h.check("failed_does_not_postpone_any_record", dates(h, name), before)
    retries = sql(
        h,
        "SELECT record_key,recheck_failures,recheck_retry_at>now() AS cooling, "
        "recheck_retry_at<=now()+interval '1 hour' AS bounded "
        "FROM seal_record WHERE source_id=%s ORDER BY record_key",
        (name,),
    )
    h.check("failure_backoff_only_selected", sum(row["cooling"] is True for row in retries), 2)
    h.check("failure_backoff_bounded", all(row["bounded"] for row in retries if row["cooling"]))
    h.check(
        "failure_counter_only_selected",
        sorted(row["recheck_failures"] for row in retries),
        [0, 0, 1, 1],
    )
    second = scheduled(h)
    h.check(
        "failed_parent_does_not_starve_other_due",
        second["seeds"][0]["url"] != first["seeds"][0]["url"],
    )
    completed(h, second)
    h.site.failed.clear()
    sql(
        h,
        "UPDATE seal_record SET recheck_retry_at=now()-interval '1 second' WHERE source_id=%s",
        (name,),
    )
    recovered = completed(h, scheduled(h))
    h.check("recovered_same_parent", recovered["seeds"], first["seeds"])
    h.check(
        "recovered_backoff_reset",
        sql(h, "SELECT max(recheck_failures) AS n FROM seal_record WHERE source_id=%s", (name,))[0][
            "n"
        ],
        0,
    )
    # One selected row; the shared page emits a second, non-due row.
    sql(h, "UPDATE seal_record SET next_check=now()+interval '1 day' WHERE source_id=%s", (name,))
    sql(
        h,
        "UPDATE seal_record SET next_check=now()-interval '1 hour' WHERE source_id=%s AND record_key='123'",
        (name,),
    )
    before = {row["record_key"]: row["next_check"] for row in dates(h, name)}
    extra = completed(h, scheduled(h))
    after = {row["record_key"]: row["next_check"] for row in dates(h, name)}
    h.check("shared_extra_record_observed", extra["report"]["record_counts"]["record_count"], 2)
    h.check("shared_extra_not_scheduled", after["124"], before["124"])
    h.check(
        "only_plan_absence_count", extra["report"]["scope_evidence"]["unobserved_record_count"], 0
    )
    # A selected row absent from a successful parent is unknown, never deleted/postponed.
    sql(
        h,
        "UPDATE seal_record SET next_check=now()-interval '1 hour' WHERE source_id=%s AND record_key='123'",
        (name,),
    )
    h.site.missing.add("123")
    before = dates(h, name)
    missing = completed(h, scheduled(h))
    h.check(
        "planned_missing_count", missing["report"]["scope_evidence"]["unobserved_record_count"], 1
    )
    h.check("missing_record_never_deleted", len(dates(h, name)), 4)
    h.check("missing_record_not_postponed", dates(h, name), before)
    h.capture(
        "planned-failure-and-recovery",
        {"failed": failed, "recovered": recovered, "missing": missing},
    )
    h.site.missing.clear()
    sql(
        h,
        "UPDATE seal_record SET next_check=now()-interval '1 hour',recheck_retry_at=NULL WHERE source_id=%s",
        (name,),
    )
    stale = scheduled(h)
    before = dates(h, name)
    retry_before = sql(
        h,
        "SELECT id,recheck_retry_at,recheck_failures FROM seal_record WHERE source_id=%s ORDER BY id",
        (name,),
    )
    h.cli("pause", name, "--reason", "fixture finished")
    h.start_worker()
    h.wait_worker()
    h.check(
        "stale_generation_cannot_complete",
        h.cli("inspect", "run", stale["id"])["run"]["status"],
        "superseded",
    )
    h.check("stale_generation_preserves_dates", dates(h, name), before)
    h.check(
        "stale_generation_preserves_retry_state",
        sql(
            h,
            "SELECT id,recheck_retry_at,recheck_failures "
            "FROM seal_record WHERE source_id=%s ORDER BY id",
            (name,),
        ),
        retry_before,
    )


def run_all(h, scenario="all"):
    old = h.site
    h.site = Site()
    try:
        version = pack(h)
        for name, case in (("identity", identity), ("batch", batch), ("failure", failure)):
            if scenario in ("all", name):
                case(h, version)
    finally:
        old.ledger.extend(h.site.ledger)
        h.site.close()
        h.site = old


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scenario", choices=["all", "identity", "batch", "failure"], default="all"
    )
    args = parser.parse_args()
    h, error = Harness(args.output), None
    try:
        h.start()
        run_all(h, args.scenario)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("planned-recheck", error)
        path = h.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(
            command="uv run --frozen python experiments/v1.2-acceptance/recheck_acceptance.py "
            f"--scenario {args.scenario} --output <new-directory>",
            preconditions="PostgreSQL 17+ tools on PATH; disposable local cluster and loopback fixture; no external DSN",
            scope="JSON detail identity and frozen-parent proof; due-only request-budget batches; failure/backoff/fencing",
        )
        path.write_text(json.dumps(manifest, indent=2))
        h.close()
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
