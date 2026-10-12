"""Run lifecycle and bounded subprocess execution shared by CLI and worker."""

import os
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone

from .core import SealError, uid
from .db import connect, j, locked_run, one
from .run_state import TERMINAL_STATES, eligibility_error, exhaustion_error


def create_run(binding_id, mode, connection=None):
    if mode not in ("collect", "recheck"):
        raise SealError("unsupported_run_mode")
    if connection is None:
        with connect() as c:
            return create_run(binding_id, mode, c)
    c = connection
    binding = one(c, "SELECT * FROM seal.binding WHERE id=%s", (binding_id,))
    source = one(c, "SELECT * FROM seal.source WHERE id=%s FOR UPDATE", (binding["source_id"],))
    if source["paused"]:
        raise SealError("source_paused")
    if source["cooldown_until"] and source["cooldown_until"] > datetime.now(timezone.utc):
        raise SealError("source_cooling_down")
    identity = uid()
    config = binding["config"]
    seeds = [{"url": url, "role": config["seed_role"]} for url in config["entry_urls"]]
    plan = None
    if mode == "recheck":
        from .recheck import plan_recheck

        seeds, plan = plan_recheck(c, source, config)
    seq = source["run_seq"] + 1
    c.execute("UPDATE seal.source SET run_seq=%s WHERE id=%s", (seq, source["id"]))
    c.execute(
        """INSERT INTO seal.run(id,source_id,binding_id,mode,namespace,generation,run_seq,deadline,seeds,recheck_plan)
        VALUES(%s,%s,%s,%s,'runtime',%s,%s,%s,%s,%s)""",
        (
            identity,
            source["id"],
            binding_id,
            mode,
            source["generation"],
            seq,
            datetime.now(timezone.utc) + timedelta(seconds=config["budget"]["seconds"] * 3 + 120),
            j(seeds),
            j(plan) if plan else None,
        ),
    )
    return identity


def end_run(connection, source, run, status, reason):
    from .manifest import terminal_report
    from .recheck import failed_dates, scope_details

    run = dict(run, report=dict(run["report"], scope_evidence=scope_details(run)))
    report = terminal_report(connection, source, run, status, reason)
    failed_dates(connection, source, run, status)
    connection.execute(
        "UPDATE seal.run SET status=%s,completed_at=now(),errors=%s,report=%s WHERE id=%s",
        (status, j(report["errors"]), j(report), run["id"]),
    )


def begin_attempt(run_id):
    with connect() as c:
        source, run = locked_run(c, run_id)
        if run["status"] in TERMINAL_STATES:
            return None
        limit = exhaustion_error(run)
        if limit:
            end_run(c, source, run, "failed", limit)
            return None
        if run["mode"] not in ("collect", "recheck"):
            raise SealError("unsupported_run_mode")
        code = eligibility_error(source, run)
        if code:
            end_run(c, source, run, "failed", code)
            return None
        if source["cooldown_until"] and source["cooldown_until"] > datetime.now(timezone.utc):
            end_run(c, source, run, "failed", "source_cooling_down")
            return None
        c.execute("UPDATE seal.source SET write_seq=%s WHERE id=%s", (run["run_seq"], source["id"]))
        # Preserve prior attempt input/report history in the report object before reset.
        history = run["report"].get("attempts", [])
        if run["attempt_epoch"]:
            history += [
                {
                    "epoch": run["attempt_epoch"],
                    "inputs": run["inputs"],
                    "errors": run["errors"],
                }
            ]
        report = {"attempts": history}
        c.execute(
            "UPDATE seal.run SET attempt_epoch=attempt_epoch+1,status='running',inputs='[]',errors='[]',report=%s WHERE id=%s",
            (j(report), run_id),
        )
        return run["attempt_epoch"] + 1


def run_context(run_id, epoch):
    with connect() as c:
        run = one(c, "SELECT * FROM seal.run WHERE id=%s AND attempt_epoch=%s", (run_id, epoch))
        binding = one(c, "SELECT * FROM seal.binding WHERE id=%s", (run["binding_id"],))
    context = {
        **run,
        "deadline": run["deadline"].isoformat(),
        "config": binding["config"],
        "params": binding["params"],
        "recipe_version": binding["recipe_version"],
    }
    return context


def run_summary(connection, run):
    from .progress import snapshot

    state = snapshot(connection, run)
    return {
        "attempt_epoch": run["attempt_epoch"],
        "counts_basis": "current_attempt; see progress contract",
        "counts": state.counts,
        "terminal": run["status"] in TERMINAL_STATES,
    }


def receipt(run_id):
    with connect() as c:
        run = one(c, "SELECT * FROM seal.run WHERE id=%s", (run_id,))
        return run_receipt(c, run)


def run_receipt(connection, run):
    """Build the same CLI shape from stored or transaction-local terminal state."""
    return {
        "run_id": run["id"],
        "source_id": run["source_id"],
        "binding_id": run["binding_id"],
        "mode": run["mode"],
        "status": run["status"],
        "attempt_epoch": run["attempt_epoch"],
        "max_attempts": run["max_attempts"],
        "deadline": run["deadline"].isoformat(),
        "errors": run["errors"],
        "report": run["report"],
        "summary": run_summary(connection, run),
    }


def _execute_attempt(run_id, progress=None):
    from .completion import completion_status, finish_run

    state = receipt(run_id)
    if state["status"] == "finishing":
        return finish_run(run_id, state["attempt_epoch"])
    epoch = begin_attempt(run_id)
    if epoch is None:
        return receipt(run_id)
    context = run_context(run_id, epoch)
    with connect() as c:
        recipe = one(
            c, "SELECT manifest FROM seal.recipe_version WHERE id=%s", (context["recipe_version"],)
        )
    python = recipe["manifest"]["python"]
    # Child stderr is not copied into artifacts: Scrapy errors may contain source data.
    process = subprocess.Popen(
        [python, "-m", "seal.crawl", run_id, str(epoch), str(os.getpid())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    timeout = min(
        context["config"]["budget"]["seconds"] + 15,
        (datetime.fromisoformat(context["deadline"]) - datetime.now(timezone.utc)).total_seconds(),
    )
    start = time.monotonic()
    cancelled = False
    try:
        while process.poll() is None:
            if time.monotonic() - start >= timeout:
                cancelled = True
                break
            with connect() as c:
                source = one(c, "SELECT * FROM seal.source WHERE id=%s", (context["source_id"],))
                current = one(c, "SELECT * FROM seal.run WHERE id=%s", (run_id,))
                if current["attempt_epoch"] != epoch or current["status"] in TERMINAL_STATES:
                    cancelled = True
                    break
                code = eligibility_error(source, current)
                if code or current["deadline"] <= datetime.now(timezone.utc):
                    # Only stopping takes write locks; polling must not contend
                    # with a valid completion transaction or its fault recovery.
                    source, current = locked_run(c, run_id)
                    code = eligibility_error(source, current)
                    expired = current["deadline"] <= datetime.now(timezone.utc)
                    if (
                        current["attempt_epoch"] == epoch
                        and current["status"] not in TERMINAL_STATES
                        and (code or expired)
                    ):
                        end_run(c, source, current, "failed", code or "deadline_exceeded")
                    cancelled = True
                    break
            if progress:
                progress.update(run_id)
            time.sleep(0.2)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    state = receipt(run_id)
    if state["status"] in TERMINAL_STATES:
        return state
    if state["status"] == "finishing":
        if state["attempt_epoch"] != epoch:
            return state
        return finish_run(run_id, epoch)
    with connect() as c:
        source, run = locked_run(c, run_id)
        if run["attempt_epoch"] == epoch and run["status"] == "running":
            reason = eligibility_error(source, run) or exhaustion_error(run)
            if reason:
                end_run(c, source, run, "failed", reason)
            else:
                code = "crawl_timeout" if cancelled else "child_process_failed"
                errors = sorted(set([*run["errors"], code]))
                status = completion_status(errors, run)
                run = dict(run, errors=errors)
                if status == "failed":
                    end_run(c, source, run, status, None)
                else:
                    c.execute(
                        "UPDATE seal.run SET status=%s,errors=%s WHERE id=%s",
                        (status, j(errors), run_id),
                    )
    return receipt(run_id)


def execute_run(run_id, progress=None):
    # Session lock survives completion transactions, but is released by a crash.
    # Recovery cannot enqueue or start a second owner while a foreground run lives.
    with connect() as owner:
        if not owner.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", (run_id,)
        ).fetchone()["acquired"]:
            raise SealError("run_already_executing")
        try:
            while True:
                result = _execute_attempt(run_id, progress)
                if result["status"] != "retryable":
                    return result
                if progress:
                    progress.update(run_id)
                time.sleep(1)
        finally:
            owner.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (run_id,))
