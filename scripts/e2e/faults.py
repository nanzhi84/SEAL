"""Real failures at public boundaries; isolated fixture DB may inject faults."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time

import psycopg


def sql(h, statement, args=None):
    with psycopg.connect(h.env["SEAL_DATABASE_URL"]) as c:
        cursor = c.execute(statement, args)
        return cursor.fetchall() if cursor.description else None


def until(predicate, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for externally observable state")


def m0_faults(h):
    # Body installation fails in the exact hash directory, while the bundle remains readable.
    h.config("disk", entries=[h.site.url + "/a/one"], seed_role="detail")
    binding = h.binding("disk")
    body = b"<h1>First notice</h1><time>2026-01-02</time><article>Public content A</article>"
    key = hashlib.sha256(body).hexdigest()
    target = h.root / "archive" / "objects" / key[:2] / key[2:]
    target.parent.mkdir(parents=True, exist_ok=True)
    saved = target.read_bytes() if target.exists() else None
    target.unlink(missing_ok=True)
    target.mkdir()
    try:
        failed = h.cli("run", "disk", "--binding", binding, ok=False)
        run = h.cli("inspect", "run", failed["run_id"])
        h.check("disk_failure_callback_blocked", len(run["results"]), 0)
        h.check("disk_failure_completion_zero", len(h.export("disk")["documents"]), 0)
    finally:
        target.rmdir()
        if saved is not None:
            target.write_bytes(saved)
    h.config("dbfail", entries=[h.site.url + "/a/one"], seed_role="detail")
    binding = h.binding("dbfail")
    sql(
        h,
        """CREATE FUNCTION e2e_reject_observation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN IF NEW.source_id='dbfail' THEN RAISE EXCEPTION 'synthetic storage fault'; END IF; RETURN NEW; END $$;
    CREATE TRIGGER e2e_observation_fault BEFORE INSERT ON seal_fetch_observation FOR EACH ROW EXECUTE FUNCTION e2e_reject_observation();""",
    )
    try:
        failed = h.cli("run", "dbfail", "--binding", binding, ok=False)
        run = h.cli("inspect", "run", failed["run_id"])
        h.check("db_failure_callback_blocked", len(run["results"]), 0)
        h.check("db_failure_no_dangling_snapshot_ref", len(run["observations"]), 0)
    finally:
        sql(
            h,
            "DROP TRIGGER e2e_observation_fault ON seal_fetch_observation; DROP FUNCTION e2e_reject_observation();",
        )
    h.config("replayfault")
    binding = h.binding("replayfault")
    original = h.cli("run", "replayfault", "--binding", binding)
    state = h.cli("inspect", "run", original["run_id"])["run"]
    before = len(h.site.ledger)
    sql(
        h,
        "UPDATE seal_run SET inputs=%s::jsonb WHERE id=%s",
        (json.dumps(state["inputs"][1:]), original["run_id"]),
    )
    failed = h.cli("replay", original["run_id"], ok=False)
    h.check("missing_replay_mapping_fails", "replay_miss" in failed["errors"])
    h.check("missing_replay_never_downloads", len(h.site.ledger), before)
    # Restore the fixture input then create an explicit conflicting mapping.
    altered = list(state["inputs"])
    altered.append(dict(altered[0], snapshot_id=altered[-1]["snapshot_id"]))
    sql(
        h,
        "UPDATE seal_run SET inputs=%s::jsonb WHERE id=%s",
        (json.dumps(altered), original["run_id"]),
    )
    failed = h.cli("replay", original["run_id"], ok=False)
    h.check("ambiguous_replay_mapping_fails", "replay_ambiguous" in failed["errors"])
    h.check("ambiguous_replay_never_downloads", len(h.site.ledger), before)


def cache_poc(h):
    for mode in ("dummy", "dont_cache", "ignore", "rfc", "fallback"):
        endpoint = "/cache/" + mode
        path = h.output / f"cache-{mode}.json"
        before = len(h.site.ledger)
        subprocess.run(
            [
                sys.executable,
                "scripts/e2e/cache_probe.py",
                h.site.url + endpoint,
                mode,
                str(h.root / ("cache-" + mode)),
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        events = json.loads(path.read_text())
        count = len(h.site.ledger) - before
        h.check(
            "native_cache_requests_" + mode,
            count,
            1 if mode == "dummy" else 2 if mode in ("rfc", "fallback") else 3,
        )
        if mode == "dummy":
            h.check("cached_500_retries_same_error", sum(e.get("cached", False) for e in events), 2)
        if mode == "rfc":
            h.check(
                "native_304_becomes_application_200",
                any(e.get("status") == 304 for e in events)
                and all(e["status"] == 200 for e in events if e["stage"] == "application_response"),
            )
        if mode == "fallback":
            h.check(
                "native_error_can_fallback_cached_200",
                any(e["stage"] == "before_cache_exception" for e in events)
                and sum(
                    e.get("status") == 200 and e["stage"] == "application_response" for e in events
                )
                == 2,
            )


def concurrency_and_recovery(h):
    h.config("fence")
    binding = h.binding("fence")
    h.select("fence", binding, 0)
    # Trigger holds the finish boundary under a real PostgreSQL advisory lock.
    sql(
        h,
        """CREATE FUNCTION e2e_hold_finish() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN IF NEW.source_id='fence' AND NEW.status='finishing' THEN PERFORM pg_advisory_xact_lock(891722); END IF; RETURN NEW; END $$;
    CREATE TRIGGER e2e_finish_hold BEFORE UPDATE ON seal_run FOR EACH ROW EXECUTE FUNCTION e2e_hold_finish();""",
    )
    blocker = psycopg.connect(h.env["SEAL_DATABASE_URL"], autocommit=True)
    blocker.execute("SELECT pg_advisory_lock(891722)")
    queued = h.cli("run", "fence", "--enqueue")
    h.start_worker()
    try:
        until(
            lambda: (
                sql(
                    h,
                    "SELECT count(*) FROM seal_result r JOIN seal_document d ON d.id=r.document_id WHERE d.source_id='fence' AND d.namespace='runtime'",
                )[0][0]
                == 2
            )
        )
        h.check("staged_results_are_not_completed", len(h.export("fence")["documents"]), 0)
        os.kill(h.worker.pid, signal.SIGKILL)
        h.worker.wait(timeout=5)
        h.worker_log.close()
        h.worker = None
        # Child monitors parent death independently and stops its own process group.
        until(
            lambda: (
                not sql(
                    h,
                    "SELECT 1 FROM pg_stat_activity WHERE wait_event='advisory' AND query LIKE 'UPDATE seal_run SET status=%'",
                )
            ),
            seconds=10,
        )
    finally:
        blocker.execute("SELECT pg_advisory_unlock(891722)")
        blocker.close()
        sql(h, "DROP TRIGGER e2e_finish_hold ON seal_run; DROP FUNCTION e2e_hold_finish();")
    # Simulate elapsed heartbeat time, never manually change Procrastinate job state.
    sql(h, "UPDATE procrastinate_workers SET last_heartbeat=now()-interval '2 minutes'")
    h.cli("recover")
    h.start_worker()
    h.wait_worker()

    final = h.cli("inspect", "run", queued["run_id"])["run"]
    h.check("killed_worker_recovered", final["status"], "complete")
    h.check("stalled_recovery_new_attempt", final["attempt_epoch"], 2)
    h.check("stalled_recovery_complete_once", len(h.export("fence")["documents"]), 2)
    # A delayed queued run loses eligibility when a newer synchronous run starts.
    old = h.cli("run", "fence", "--enqueue")
    h.cli("run", "fence")
    h.start_worker()
    h.wait_worker()
    h.check(
        "old_run_cannot_reclaim_write_seq",
        h.cli("inspect", "run", old["run_id"])["run"]["status"],
        "superseded",
    )
    history = h.cli("inspect", "source", "fence")
    h.check("recovered_identical_content_no_extra_revision", len(history["revisions"]), 2)
    # Due cursor and queue insertion are one transaction; duplicate wake-ups coalesce.
    sql(h, "UPDATE seal_source SET next_poll=now()-interval '1 day' WHERE id='fence'")
    first = h.cli("schedule")
    second = h.cli("schedule")
    h.check("duplicate_scheduler_wakeup_coalesced", len(second["run_ids"]), 0)
    h.check("scheduler_persisted_jobs", len(first["run_ids"]) > 0)
    h.start_worker()
    h.wait_worker()


def delayed_fencing(h):
    for kind in ("run", "attempt", "generation"):
        name = "delay_" + kind
        h.site.version = "A"
        h.config(name, entries=[h.site.url + f"/{name}/one"], seed_role="detail")
        binding = h.binding(name)
        h.select(name, binding, 0)
        h.site.delay_started.clear()
        h.site.delay_release.clear()
        h.site.delay_next = True
        old = subprocess.Popen(
            [sys.executable, "-m", "seal", "run", name],
            env=h.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            if not h.site.delay_started.wait(timeout=10):
                raise AssertionError("delayed source was never reached")
            old_run = sql(
                h,
                "SELECT id FROM seal_run WHERE source_id=%s AND mode='collect' ORDER BY created_at DESC LIMIT 1",
                (name,),
            )[0][0]
            h.site.version = "B"
            if kind == "attempt":
                h.cli("retry", old_run)
            else:
                if kind == "generation":
                    h.select(name, binding, 1)
                h.cli("run", name)
            h.site.delay_release.set()
            stdout, stderr = old.communicate(timeout=15)
            h.receipts.append(
                {
                    "args": ["run", name, "delayed"],
                    "exit": old.returncode,
                    "result": json.loads(stdout),
                }
            )
            h.check(
                "late_old_" + kind + "_does_not_overwrite",
                h.export(name)["documents"][0]["body"],
                "Public content B",
            )
            observations = sql(
                h,
                "SELECT eligible FROM seal_fetch_observation WHERE run_id=%s AND attempt_epoch=1 AND url LIKE %s AND status=200",
                (old_run, "%/one"),
            )
            h.check("late_old_" + kind + "_not_eligible", all(not r[0] for r in observations))
        finally:
            h.site.delay_release.set()
            if old.poll() is None:
                old.terminate()
                old.wait(timeout=10)
    h.site.version = "A"
