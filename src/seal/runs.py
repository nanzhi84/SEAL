"""Run lifecycle and bounded subprocess execution shared by CLI and worker."""

import os
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone

from .core import Objects, SealError, uid
from .db import connect, j, locked_run, one


def freeze_replay_origin(connection, original):
    """Freeze negative inputs without turning Replay into an input whitelist.

    An unchanged callback can rediscover a candidate that the original attempt
    deliberately refused before any Snapshot existed. Only those exact historical
    failures may be skipped; an unknown request must still be a Replay miss.
    """
    inherited = original["report"].get("replay_origin_id")
    if inherited:
        return inherited
    available = connection.execute("SELECT to_regclass('seal_discovery') AS name").fetchone()[
        "name"
    ]
    candidates = (
        connection.execute(
            """SELECT d.fingerprint,d.role,d.parent_snapshot_id,d.reason
               FROM seal_discovery d WHERE d.run_id=%s AND d.attempt_epoch=%s
                 AND d.snapshot_id IS NULL AND d.state IN ('failed','skipped')
                 AND d.reason IS NOT NULL
                 AND d.reason NOT IN ('duplicate_request','transport_continued',
                                      'replay_miss','replay_ambiguous')
                 AND NOT EXISTS (
                     SELECT 1 FROM seal_discovery archived
                     JOIN seal_fetch_observation observed ON observed.id=archived.observation_id
                     WHERE archived.run_id=d.run_id
                       AND archived.attempt_epoch=d.attempt_epoch
                       AND archived.role=d.role AND archived.snapshot_id IS NOT NULL
                       AND observed.final_url IS NOT NULL
                       AND (archived.fingerprint=d.fingerprint
                            OR (d.chain_id IS NOT NULL AND archived.chain_id=d.chain_id)))
               ORDER BY d.created_at,d.id""",
            (original["id"], original["attempt_epoch"]),
        ).fetchall()
        if available
        else []
    )
    if not candidates:
        return None
    reasons = {item["reason"] for item in candidates}
    value = {
        "contract": 1,
        "run_id": original["id"],
        "attempt_epoch": original["attempt_epoch"],
        "status": original["status"],
        "errors": sorted(set(original["errors"]) & reasons),
        "rejected_candidates": candidates,
    }
    return Objects().put_json(value)


def collect_source(source_id, binding_id=None, *, enqueue=False, recheck=False):
    """Start a persisted Source run through the same lifecycle as the operator CLI."""
    with connect() as c:
        source = one(c, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (source_id,))
        binding_id = binding_id or source["binding_id"]
        if not binding_id:
            raise SealError("binding_required")
        binding = one(c, "SELECT source_id FROM seal_binding WHERE id=%s", (binding_id,))
        if binding["source_id"] != source_id:
            raise SealError("binding_source_mismatch")
        run_id = create_run(binding_id, "recheck" if recheck else "collect", c)
        if enqueue:
            from .queue import enqueue as enqueue_run

            enqueue_run(c, run_id)
    return receipt(run_id) if enqueue else execute_run(run_id)


def create_run(binding_id, mode, connection=None, replay_from=None, slot=None, due_only=False):
    if mode not in ("collect", "recheck", "replay"):
        raise SealError("unsupported_run_mode")
    if connection is None:
        with connect() as c:
            return create_run(binding_id, mode, c, replay_from, slot, due_only)
    c = connection
    binding = one(c, "SELECT * FROM seal_binding WHERE id=%s", (binding_id,))
    source = one(c, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (binding["source_id"],))
    if slot:
        existing = c.execute("SELECT id FROM seal_run WHERE slot=%s", (slot,)).fetchone()
        if existing:
            return existing["id"]
    online = mode != "replay"
    if online and source["paused"]:
        raise SealError("source_paused")
    if (
        online
        and source["cooldown_until"]
        and source["cooldown_until"] > datetime.now(timezone.utc)
    ):
        raise SealError("source_cooling_down")
    identity = uid()
    config = binding["config"]
    seeds = [{"url": url, "role": config["seed_role"]} for url in config["entry_urls"]]
    replay_inputs = []
    replay_origin_id = None
    recheck_plan = None
    if mode == "recheck":
        from .recheck import plan_recheck

        seeds, recheck_plan = plan_recheck(c, source, config, due_only)
    if mode == "replay":
        original = one(c, "SELECT * FROM seal_run WHERE id=%s", (replay_from,))
        if original["source_id"] != source["id"]:
            raise SealError("cross_source_replay_rejected")
        replay_origin_id = freeze_replay_origin(c, original)
        if not original["inputs"] and not replay_origin_id:
            raise SealError("replay_inputs_missing")
        seeds, replay_inputs = original["seeds"], original["inputs"]
        recheck_plan = original.get("recheck_plan")
    seq = source["run_seq"] + 1 if online else 0
    if online:
        c.execute("UPDATE seal_source SET run_seq=%s WHERE id=%s", (seq, source["id"]))
    c.execute(
        """INSERT INTO seal_run(id,source_id,binding_id,mode,namespace,generation,run_seq,deadline,seeds,replay_inputs,slot,recheck_plan,report)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            identity,
            source["id"],
            binding_id,
            mode,
            "runtime" if online else mode + ":" + identity,
            source["generation"],
            seq,
            datetime.now(timezone.utc) + timedelta(seconds=config["budget"]["seconds"] * 3 + 120),
            j(seeds),
            j(replay_inputs),
            slot,
            j(recheck_plan) if recheck_plan is not None else None,
            j({"replay_origin_id": replay_origin_id} if replay_origin_id else {}),
        ),
    )
    return identity


def end_run(connection, source, run, status, reason):
    from .manifest import terminal_report
    from .recheck import failed_dates, scope_details, unobserved_records

    unobserved = unobserved_records(connection, source, run)
    scope = dict(
        run["report"].get("scope_evidence", {}),
        **scope_details(run),
        unobserved_records=unobserved,
        unobserved_record_count=len(unobserved),
    )
    run = dict(run, report=dict(run["report"], scope_evidence=scope))
    report = terminal_report(connection, source, run, status, reason)
    failed_dates(connection, source, run, status)
    connection.execute(
        "UPDATE seal_run SET status=%s,completed_at=now(),errors=%s,report=%s WHERE id=%s",
        (status, j(report["errors"]), j(report), run["id"]),
    )


def begin_attempt(run_id):
    with connect() as c:
        source, run = locked_run(c, run_id)
        if run["status"] in ("complete", "superseded", "partial", "failed"):
            return None
        if run["attempt_epoch"] >= run["max_attempts"] or run["deadline"] <= datetime.now(
            timezone.utc
        ):
            end_run(c, source, run, "failed", "attempts_or_deadline_exhausted")
            return None
        if run["mode"] not in ("collect", "recheck", "replay"):
            raise SealError("unsupported_run_mode")
        if run["mode"] != "replay":
            if (
                source["generation"] != run["generation"]
                or source["paused"]
                or source["write_seq"] > run["run_seq"]
            ):
                end_run(c, source, run, "superseded", "superseded")
                return None
            if source["cooldown_until"] and source["cooldown_until"] > datetime.now(timezone.utc):
                end_run(c, source, run, "failed", "source_cooling_down")
                return None
            c.execute(
                "UPDATE seal_source SET write_seq=%s WHERE id=%s", (run["run_seq"], source["id"])
            )
        # Preserve prior attempt input/report history in the report object before reset.
        history = run["report"].get("attempts", [])
        if run["attempt_epoch"]:
            history += [
                {
                    "epoch": run["attempt_epoch"],
                    "inputs": run["inputs"],
                    "errors": run["errors"],
                    "results": run["result_ids"],
                }
            ]
        report = {"attempts": history}
        errors = []
        if run["report"].get("replay_origin_id"):
            report["replay_origin_id"] = run["report"]["replay_origin_id"]
            errors = Objects().json(report["replay_origin_id"])["errors"]
        c.execute(
            "UPDATE seal_run SET attempt_epoch=attempt_epoch+1,status='running',inputs='[]',result_ids='[]',refs='[]',errors=%s,report=%s WHERE id=%s",
            (j(errors), j(report), run_id),
        )
        return run["attempt_epoch"] + 1


def run_context(run_id, epoch):
    with connect() as c:
        run = one(c, "SELECT * FROM seal_run WHERE id=%s AND attempt_epoch=%s", (run_id, epoch))
        binding = one(c, "SELECT * FROM seal_binding WHERE id=%s", (run["binding_id"],))
    context = {
        **run,
        "deadline": run["deadline"].isoformat(),
        "config": binding["config"],
        "params": binding["params"],
        "recipe_version": binding["recipe_version"],
    }
    if run["report"].get("replay_origin_id"):
        context["replay_origin"] = Objects().json(run["report"]["replay_origin_id"])
    return context


def run_termination(run):
    """Describe an attempt's stop without interpreting queue exhaustion as coverage."""
    status = run["status"]
    terminal = status in {"complete", "partial", "failed", "superseded"}
    if not terminal and status != "retryable":
        return {"terminal": False, "termination": None, "terminal_reason": None}
    report = run.get("report", {})
    errors = set(run.get("errors", [])) | set(report.get("errors", []))
    reason = report.get("terminal_reason")
    budget = sorted(
        code for code in errors if "budget_exceeded" in code or code == "deadline_exceeded"
    )
    if report.get("finish_reason") == "closespider_timeout":
        budget.append("closespider_timeout")
    blocked = sorted(
        errors
        & {
            "robots_denied",
            "robots_policy_http_denied",
            "robots_unavailable",
            "source_rate_limited",
            "access_control_detected",
        }
    )
    if status == "complete":
        category, reason = "queue_exhausted", reason or "queue_exhausted"
    elif status == "superseded":
        category, reason = "superseded", reason or "superseded"
    elif budget:
        category, reason = "budget_exhausted", reason or budget[0]
    elif blocked:
        category, reason = "blocked", reason or blocked[0]
    elif status == "retryable":
        category, reason = "retryable", reason or (sorted(errors)[0] if errors else status)
    else:
        category = status
        finish_reason = report.get("finish_reason")
        reason = reason or (
            finish_reason
            if finish_reason and finish_reason != "finished"
            else sorted(errors)[0]
            if errors
            else status
        )
    return {"terminal": terminal, "termination": category, "terminal_reason": reason}


def run_summary(connection, run):
    """Read current-attempt counters; immutable completion reports stay unchanged."""
    from .discovery import summarize_discovery
    from .manifest import has_table

    discovery = summarize_discovery(connection, run)
    observations = connection.execute(
        """SELECT snapshot_id FROM seal_fetch_observation
           WHERE run_id=%s AND attempt_epoch=%s AND snapshot_id IS NOT NULL""",
        (run["id"], run["attempt_epoch"]),
    ).fetchall()
    snapshots = {row["snapshot_id"] for row in observations} | {
        entry["snapshot_id"] for entry in run["inputs"]
    }
    records = (
        connection.execute(
            """SELECT count(DISTINCT record_id) AS records,count(*) AS emissions
               FROM seal_record_emission WHERE run_id=%s AND attempt_epoch=%s""",
            (run["id"], run["attempt_epoch"]),
        ).fetchone()
        if has_table(connection, "seal_record_emission")
        else {"records": None, "emissions": None}
    )
    attachments = {
        entry["snapshot_id"]
        for entry in run["inputs"]
        if entry["role"] == "attachment" or entry.get("resource_type") == "attachment"
    }
    if has_table(connection, "seal_discovery"):
        attachments.update(
            row["snapshot_id"]
            for row in connection.execute(
                """SELECT DISTINCT snapshot_id FROM seal_discovery WHERE run_id=%s
                   AND attempt_epoch=%s AND role='attachment' AND snapshot_id IS NOT NULL""",
                (run["id"], run["attempt_epoch"]),
            ).fetchall()
        )
    return {
        "attempt_epoch": run["attempt_epoch"],
        "counts_basis": "current_attempt; records are emitted identities; attachments are snapshots",
        "discovery_instrumented": discovery["instrumented"],
        "counts": {
            **{
                key: discovery[key] if discovery["instrumented"] else None
                for key in (
                    "discovered",
                    "requested",
                    "archived",
                    "failed",
                    "skipped",
                    "deduplicated",
                    "pending",
                )
            },
            "snapshots": len(snapshots),
            "records": records["records"],
            "record_emissions": records["emissions"],
            "record_outputs": len(run["report"].get("record_outputs", [])),
            "attachments": len(attachments),
            "http_attempts": discovery["http_attempts"],
            "archived_observations": discovery["archived_observations"],
        },
        "http_attempts_basis": discovery["http_attempts_basis"],
        **run_termination(run),
    }


def receipt(run_id):
    with connect() as c:
        run = one(c, "SELECT * FROM seal_run WHERE id=%s", (run_id,))
        summary = run_summary(c, run)
    return {
        "run_id": run_id,
        "source_id": run["source_id"],
        "binding_id": run["binding_id"],
        "mode": run["mode"],
        "status": run["status"],
        "attempt_epoch": run["attempt_epoch"],
        "max_attempts": run["max_attempts"],
        "deadline": run["deadline"].isoformat(),
        "errors": run["errors"],
        "report": run["report"],
        "summary": summary,
    }


def execute_run(run_id):
    from .completion import finish_run

    state = receipt(run_id)
    if state["status"] == "finishing":
        return finish_run(run_id, state["attempt_epoch"])
    epoch = begin_attempt(run_id)
    if epoch is None:
        return receipt(run_id)
    context = run_context(run_id, epoch)
    with connect() as c:
        recipe = one(
            c, "SELECT manifest FROM seal_recipe_version WHERE id=%s", (context["recipe_version"],)
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
                source = one(
                    c,
                    "SELECT generation,paused FROM seal_source WHERE id=%s",
                    (context["source_id"],),
                )
            if context["mode"] != "replay" and (
                source["generation"] != context["generation"] or source["paused"]
            ):
                cancelled = True
                break
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
    if state["status"] in ("complete", "partial", "failed", "superseded"):
        return state
    if state["status"] == "finishing":
        return finish_run(run_id, epoch)
    with connect() as c:
        source, run = locked_run(c, run_id)
        exhausted = epoch >= context["max_attempts"] or datetime.now(
            timezone.utc
        ) >= datetime.fromisoformat(context["deadline"])
        if run["attempt_epoch"] == epoch and run["status"] == "running":
            reason = "crawl_cancelled" if cancelled else "child_process_failed"
            if exhausted:
                end_run(c, source, run, "failed", reason)
            else:
                c.execute(
                    "UPDATE seal_run SET status='retryable',errors=errors || %s WHERE id=%s",
                    (j([reason]), run_id),
                )
    return receipt(run_id)
