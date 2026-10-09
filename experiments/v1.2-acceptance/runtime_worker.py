"""Persistent Worker, stale callback fencing and migration behavior acceptance."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import psycopg
from runtime_support import by_key, record_source, run, sql


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
