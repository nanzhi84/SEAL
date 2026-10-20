"""Bounded subprocess lifecycle; Source session lock and epoch fencing protect writers."""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from .core import SealError, digest, uid
from .db import connect, j, locked_run, one
from .parsing import manifest, require_runtime
from .run_state import TERMINAL_STATES, eligibility_error, exhaustion_error


def create_run(source_id, mode, connection):
    c = connection
    source = one(c, "SELECT * FROM seal.source WHERE id=%s FOR UPDATE", (source_id,))
    if source["paused"]:
        raise SealError("source_paused")
    if source["cooldown_until"] and source["cooldown_until"] > datetime.now(timezone.utc):
        raise SealError("source_cooling_down")
    config = source["config"]
    identity = uid()
    plan = reparse_plan = None
    if mode == "recheck":
        from .recheck import plan_recheck

        plan = plan_recheck(c, source, config)
    elif mode == "reparse":
        from .reparse import plan_reparse

        reparse_plan = plan_reparse(c, source, config)
    seq = source["run_seq"] + 1
    c.execute("UPDATE seal.source SET run_seq=%s WHERE id=%s", (seq, source_id))
    c.execute(
        """INSERT INTO seal.run(id,source_id,mode,generation,run_seq,deadline,
      recheck_plan,reparse_plan,config,config_hash,parser_manifest)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            identity,
            source_id,
            mode,
            source["generation"],
            seq,
            datetime.now(timezone.utc) + timedelta(seconds=config["budget"]["seconds"] * 3 + 120),
            j(plan) if plan is not None else None,
            j(reparse_plan) if reparse_plan is not None else None,
            j(config),
            digest(config),
            j(manifest(config)),
        ),
    )
    return identity


def end_run(c, source, run, status, reason):
    from .progress import terminal_report

    c.execute(
        """UPDATE seal.acquisition SET state=%s,outcome=%s,error=%s,
        acquired_at=clock_timestamp() WHERE run_id=%s AND state='running'""",
        (
            "cancelled" if reason == "interrupted" else "abandoned",
            "cancelled" if reason == "interrupted" else "abandoned",
            reason or "attempt_ended",
            run["id"],
        ),
    )
    report = terminal_report(c, run, status, reason)
    c.execute(
        "UPDATE seal.run SET status=%s,completed_at=now(),errors=%s,report=%s WHERE id=%s",
        (status, j(report["errors"]), j(report), run["id"]),
    )


def begin_attempt(run_id):
    with connect() as c:
        source, run = locked_run(c, run_id)
        if run["status"] in TERMINAL_STATES:
            return None
        code = exhaustion_error(run) or eligibility_error(source, run)
        if code:
            end_run(c, source, run, "failed", code)
            return None
        if not run["config"]:
            raise SealError("historical_run_not_resumable")
        if digest(run["config"]) != run["config_hash"]:
            raise SealError("config_snapshot_corrupt")
        if run["parser_manifest"] is None:
            if run["attempt_epoch"]:
                end_run(c, source, run, "failed", "legacy_run_parser_unknown_requires_new_run")
                return None
            frozen = manifest(run["config"])
            c.execute("UPDATE seal.run SET parser_manifest=%s WHERE id=%s", (j(frozen), run_id))
        else:
            try:
                require_runtime(run["config"], run["parser_manifest"])
            except SealError as exc:
                end_run(c, source, run, "failed", exc.code)
                return None
        c.execute("UPDATE seal.source SET write_seq=%s WHERE id=%s", (run["run_seq"], source["id"]))
        f = one(c, "SELECT * FROM seal.frontier WHERE source_id=%s FOR UPDATE", (source["id"],))
        if f["run_id"] != run_id:
            if f["run_id"]:
                previous = one(c, "SELECT * FROM seal.run WHERE id=%s FOR UPDATE", (f["run_id"],))
                if previous["status"] not in TERMINAL_STATES:
                    end_run(c, source, previous, "failed", "run_superseded")
            # A new Run is never a resume: its tasks and checkpoint start empty. The
            # previous Run keeps its own persistent tasks and acquisition facts.
            c.execute(
                "UPDATE seal.frontier SET run_id=%s,checkpoint='{}',updated_at=now() WHERE id=%s",
                (run_id, f["id"]),
            )
        # A taken-over attempt may not finish its own running acquisitions.
        c.execute(
            """UPDATE seal.acquisition SET state='abandoned',acquired_at=clock_timestamp(),
            outcome='abandoned',error='attempt_superseded' WHERE run_id=%s AND state='running'""",
            (run_id,),
        )
        c.execute(
            "UPDATE seal.run SET attempt_epoch=attempt_epoch+1,status='running',errors=(SELECT COALESCE(jsonb_agg(value),'[]'::jsonb) FROM jsonb_array_elements(errors) WHERE value #>> '{}' NOT IN ('interrupted','child_process_failed')) WHERE id=%s",
            (run_id,),
        )
        return run["attempt_epoch"] + 1


def run_context(run_id, epoch):
    with connect() as c:
        run = one(c, "SELECT * FROM seal.run WHERE id=%s AND attempt_epoch=%s", (run_id, epoch))
        frontier = one(c, "SELECT * FROM seal.frontier WHERE source_id=%s", (run["source_id"],))
        return dict(run, deadline=run["deadline"].isoformat(), checkpoint=frontier["checkpoint"])


def run_receipt(c, run):
    from .progress import snapshot

    counts = snapshot(c, run).counts
    return {
        "contract": "seal.receipt.v4",
        "run_id": run["id"],
        "source_id": run["source_id"],
        "config_hash": run["config_hash"],
        "parser_manifest": run["parser_manifest"],
        "mode": run["mode"],
        "status": run["status"],
        "attempt_epoch": run["attempt_epoch"],
        "max_attempts": run["max_attempts"],
        "deadline": run["deadline"].isoformat(),
        "errors": run["errors"],
        "report": run["report"],
        "summary": {
            "counts": counts,
            "counts_basis": "run tasks, acquisitions and deliveries; resumed deliveries deduplicated",
            "terminal": run["status"] in TERMINAL_STATES,
        },
    }


def receipt(run_id):
    with connect() as c:
        return run_receipt(c, one(c, "SELECT * FROM seal.run WHERE id=%s", (run_id,)))


def stop_process(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            # PDF processes own separate groups; allow their bounded group cleanup
            # to finish before killing the crawl parent that is responsible for them.
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def _execute_attempt(run_id, owner, progress, cancel_event=None):
    from .completion import finish_run
    from .objects import work_directory

    epoch = begin_attempt(run_id)
    if epoch is None:
        return receipt(run_id)
    context = run_context(run_id, epoch)
    # Crawl4AI's Markdown generator captures database/log paths at import time.
    # Give the child its own work directory rather than a home-directory cache.
    sdk_work = work_directory() / "sdk" / run_id / str(epoch)
    sdk_work.mkdir(parents=True, exist_ok=True, mode=0o700)
    process = subprocess.Popen(
        [sys.executable, "-m", "seal.runtime_process", run_id, str(epoch), str(os.getpid())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "CRAWL4_AI_BASE_DIRECTORY": str(sdk_work)},
    )
    start = time.monotonic()
    reason = None
    try:
        while process.poll() is None:
            # Lost session ownership must stop the still-live child, even before takeover.
            owner.execute("SELECT 1")
            if cancel_event is not None and cancel_event.is_set():
                reason = "interrupted"
                break
            if time.monotonic() - start > context["config"]["budget"]["seconds"] + 15:
                reason = "crawl_time_budget_exceeded"
                break
            with connect() as c:
                source = one(c, "SELECT * FROM seal.source WHERE id=%s", (context["source_id"],))
                run = one(c, "SELECT * FROM seal.run WHERE id=%s", (run_id,))
                reason = eligibility_error(source, run)
                if run["attempt_epoch"] != epoch or run["status"] in TERMINAL_STATES:
                    reason = "stale_attempt"
                if reason:
                    break
            if progress:
                progress.update(run_id)
            time.sleep(0.2)
    finally:
        stop_process(process)
    state = receipt(run_id)
    if reason == "interrupted":
        with connect() as c:
            source, run = locked_run(c, run_id)
            if run["status"] not in TERMINAL_STATES and run["attempt_epoch"] == epoch:
                end_run(c, source, run, "retryable", "interrupted")
        return receipt(run_id)
    if state["status"] == "finishing":
        return finish_run(run_id, epoch)
    with connect() as c:
        source, run = locked_run(c, run_id)
        if run["status"] in TERMINAL_STATES or run["attempt_epoch"] != epoch:
            return run_receipt(c, run)
        reason = reason or "child_process_failed"
        status = (
            "failed"
            if exhaustion_error(run) or reason not in {"child_process_failed", "interrupted"}
            else "retryable"
        )
        end_run(c, source, run, status, reason)
    return receipt(run_id)


def execute_run(run_id, progress=None, cancel_event=None):
    with connect() as owner:
        source_id = one(owner, "SELECT source_id FROM seal.run WHERE id=%s", (run_id,))["source_id"]
        keys = [run_id, "frontier:" + source_id]
        acquired = []
        try:
            for key in keys:
                if not owner.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", (key,)
                ).fetchone()["acquired"]:
                    raise SealError("frontier_already_executing")
                acquired.append(key)
            owner.commit()
            # Advisory locks belong to this session; heartbeat probes must not keep
            # an idle SQL transaction open while the child performs external I/O.
            owner.autocommit = True
            while True:
                result = _execute_attempt(run_id, owner, progress, cancel_event)
                if result["status"] != "retryable" or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    return result
                time.sleep(1)
        finally:
            if not owner.closed:
                for key in reversed(acquired):
                    owner.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
