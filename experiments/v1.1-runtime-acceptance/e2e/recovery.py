"""Crash windows around durable bodies and committed completion."""

import os
import signal
import subprocess
import sys

import psycopg

from .faults import sql, until


def stop_worker(h):
    os.kill(h.worker.pid, signal.SIGKILL)
    h.worker.wait(timeout=5)
    h.worker_log.close()
    h.worker = None


def crash_boundaries(h):
    for boundary in ("body", "completed"):
        name = "crash_" + boundary
        h.config(name)
        binding = h.binding(name)
        h.select(name, binding, 0)
        if boundary == "body":
            table, trigger = "seal_fetch_observation", "BEFORE INSERT"
            condition = "NEW.source_id='crash_body' AND NEW.url LIKE '%/one'"
        else:
            table, trigger = "procrastinate_jobs", "BEFORE UPDATE"
            condition = "NEW.status='succeeded' AND NEW.task_name='seal.crawl'"
        sql(
            h,
            f"""CREATE FUNCTION e2e_boundary_hold() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF {condition} THEN PERFORM pg_advisory_xact_lock(891733); END IF; RETURN NEW; END $$;
        CREATE TRIGGER e2e_boundary_hold {trigger} ON {table} FOR EACH ROW EXECUTE FUNCTION e2e_boundary_hold();""",
        )
        blocker = psycopg.connect(h.env["SEAL_DATABASE_URL"], autocommit=True)
        blocker.execute("SELECT pg_advisory_lock(891733)")
        before_objects = len(list((h.root / "archive" / "objects").glob("*/*")))
        queued = h.cli("run", name, "--enqueue")
        h.start_worker()
        try:
            blocked = until(
                lambda: sql(h, "SELECT pid FROM pg_stat_activity WHERE wait_event='advisory'")
            )
            if boundary == "body":
                h.check(
                    "body_durable_before_db_commit",
                    len(list((h.root / "archive" / "objects").glob("*/*"))) >= before_objects,
                )
                h.check("uncommitted_body_not_completed", len(h.export(name)["documents"]), 0)
            else:
                h.check("completion_before_job_ack", len(h.export(name)["documents"]), 2)
            stop_worker(h)
            until(
                lambda run_id=queued["run_id"]: (
                    f"-m seal.crawl {run_id} "
                    not in subprocess.run(
                        ["ps", "-axo", "command"], check=True, capture_output=True, text=True
                    ).stdout
                ),
                seconds=8,
            )
            # PostgreSQL can continue an in-flight statement after client SIGKILL.
            # Close that isolated backend too, modeling lost acknowledgement before commit.
            for (pid,) in blocked:
                sql(h, "SELECT pg_terminate_backend(%s)", (pid,))
            until(
                lambda: not sql(h, "SELECT 1 FROM pg_stat_activity WHERE wait_event='advisory'"),
                seconds=10,
            )
        finally:
            blocker.execute("SELECT pg_advisory_unlock(891733)")
            blocker.close()
            sql(h, f"DROP TRIGGER e2e_boundary_hold ON {table}; DROP FUNCTION e2e_boundary_hold();")
        before_requests = len(h.site.ledger)
        sql(h, "UPDATE procrastinate_workers SET last_heartbeat=now()-interval '2 minutes'")
        h.cli("recover")
        h.start_worker()
        h.wait_worker()
        run = h.cli("inspect", "run", queued["run_id"])["run"]
        h.check(boundary + "_crash_recovered", run["status"], "complete")
        h.check(boundary + "_crash_completion_count", len(h.export(name)["documents"]), 2)
        events = sql(
            h,
            "SELECT jsonb_array_length(report->'outputs') FROM seal_run WHERE source_id=%s AND status='complete' ORDER BY run_seq DESC LIMIT 1",
            (name,),
        )[0][0]
        h.check(boundary + "_crash_no_duplicate_events", events, 2)
        if boundary == "completed":
            h.check("committed_run_not_downloaded_again", len(h.site.ledger), before_requests)


def transaction_and_retry_limits(h):
    # Exercise actual external-connection defer under a rollback, not a fake queue.
    script = """
from seal.db import connect
from seal.runs import create_run
from seal.queue import enqueue
try:
    with connect() as c:
        binding=c.execute("SELECT binding_id FROM seal_source WHERE id='fence'").fetchone()['binding_id']
        run=create_run(binding,'collect',c)
        enqueue(c,run)
        raise RuntimeError('synthetic rollback')
except RuntimeError:
    pass
"""
    before = sql(
        h, "SELECT (SELECT count(*) FROM seal_run), (SELECT count(*) FROM procrastinate_jobs)"
    )[0]
    subprocess.run([sys.executable, "-c", script], env=h.env, check=True, capture_output=True)
    after = sql(
        h, "SELECT (SELECT count(*) FROM seal_run), (SELECT count(*) FROM procrastinate_jobs)"
    )[0]
    h.check("business_and_queue_rollback_together", after, before)
    h.config("bounded")
    binding = h.binding("bounded")
    h.select("bounded", binding, 0)
    h.site.failure = "page"
    first = h.cli("run", "bounded", ok=False)
    second = h.cli("retry", first["run_id"], ok=False)
    third = h.cli("retry", first["run_id"], ok=False)
    h.check("transient_failure_retryable", first["status"], "retryable")
    h.check(
        "logical_run_has_three_attempt_limit",
        (second["attempt_epoch"], third["attempt_epoch"], third["status"]),
        (2, 3, "failed"),
    )
    h.cli("retry", first["run_id"], ok=False)
    h.check(
        "manual_retry_does_not_reset_limit",
        h.cli("inspect", "run", first["run_id"])["run"]["attempt_epoch"],
        3,
    )
    h.site.failure = None
