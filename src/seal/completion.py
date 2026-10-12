"""Completion commits document pointers; technical success never proves coverage."""

from .core import Objects, SealError
from .db import connect, fenced, j, locked_run
from .documents import accept_documents, finish_documents
from .manifest import terminal_report
from .recheck import complete_dates, failed_dates
from .run_state import TERMINAL_STATES, exhaustion_error


def completion_status(errors, run):
    limit = exhaustion_error(run)
    if limit == "deadline_exceeded" and limit not in errors:
        errors.append(limit)
    if not errors:
        return "complete"
    structural = {"unresolved_seed", "incomplete_crawl", "document_parse_missing"}
    temporary = {"download_failed", "http_5xx", "child_process_failed", "crawl_timeout"}
    if set(errors) & temporary and not (set(errors) - structural - temporary) and not limit:
        return "retryable"
    if limit and limit not in errors:
        errors.append(limit)
    return "failed"


def finish_run(run_id, epoch=None):
    from .runs import receipt, run_receipt

    with connect() as c:
        source, run = locked_run(c, run_id)
        epoch = run["attempt_epoch"] if epoch is None else epoch
        if run["status"] in TERMINAL_STATES:
            return run_receipt(c, run)
        if run["mode"] not in ("collect", "recheck"):
            raise SealError("unsupported_run_mode")
        if epoch != run["attempt_epoch"]:
            raise SealError("stale_attempt")
        if run["status"] != "finishing":
            raise SealError("crawl_not_completed")
        try:
            fenced(source, run, epoch)
        except SealError as exc:
            report = terminal_report(c, source, run, "failed", exc.code)
            c.execute(
                "UPDATE seal.run SET status='failed',report=%s,errors=%s,completed_at=now() WHERE id=%s",
                (j(report), j(report["errors"]), run_id),
            )
            return run_receipt(
                c, dict(run, status="failed", errors=report["errors"], report=report)
            )
        errors = set(run["errors"])
        used = {(entry["logical_url"], entry["role"]) for entry in run["inputs"]}
        if not all((s["url"], s["role"]) in used for s in run["seeds"]):
            errors.add("unresolved_seed")
        exchanges = c.execute(
            """SELECT DISTINCT ON(o.chain_id) o.status,o.error,o.final_url,
            EXISTS(SELECT 1 FROM seal.discovery d WHERE d.run_id=o.run_id AND d.attempt_epoch=o.attempt_epoch
              AND d.chain_id=o.chain_id AND (d.role='robots' OR d.reason='optional_entry_missing')) AS absent_allowed
            FROM seal.fetch_observation o WHERE o.run_id=%s AND o.attempt_epoch=%s
            ORDER BY o.chain_id,o.requested_at DESC""",
            (run_id, epoch),
        ).fetchall()
        for exchange in exchanges:
            status = exchange["status"]
            if exchange["absent_allowed"] and status in {404, 410}:
                continue
            if status is None:
                errors.add("download_failed")
            elif status == 429:
                errors.add("source_rate_limited")
            elif status >= 500:
                errors.add("http_5xx")
            elif status >= 400:
                errors.add("http_error")
            elif status == 304:
                errors.add("unexpected_304")
            elif 300 <= status < 400 and exchange["final_url"] is None:
                errors.add("redirect_incomplete")
        finish_reason = run["report"].get("finish_reason")
        if finish_reason != "finished":
            errors.add("incomplete_crawl")
        if finish_reason == "closespider_timeout":
            errors.add("crawl_time_budget_exceeded")
        for entry in run["inputs"]:
            try:
                body = Objects().json(entry["snapshot_id"])
                Objects().get(body["body_hash"])
            except SealError as exc:
                errors.add(exc.code)
        documents = finish_documents(c, source, run)
        errors.update(documents["errors"])
        missing = c.execute(
            """SELECT count(*) AS n FROM seal.fetch_observation o WHERE o.run_id=%s AND o.attempt_epoch=%s
            AND o.is_content AND o.processing_stage<>'parse_failed' AND NOT EXISTS (
             SELECT 1 FROM seal.document_emission e WHERE e.run_id=o.run_id AND e.attempt_epoch=o.attempt_epoch AND e.observation_id=o.id)""",
            (run_id, epoch),
        ).fetchone()["n"]
        if missing:
            errors.add("document_parse_missing")
        run["errors"] = sorted(errors)
        status = completion_status(run["errors"], run)
        report = terminal_report(c, source, run, status, None)
        errors = report["errors"]
        status = completion_status(errors, run)
        if status == "complete":
            accept_documents(c, source, run, documents["outputs"])
            complete_dates(c, source, run, documents["outputs"])
        else:
            failed_dates(c, source, run, status)
        c.execute(
            "UPDATE seal.run SET status=%s,report=%s,errors=%s,completed_at=now() WHERE id=%s",
            (status, j(report), j(errors), run_id),
        )
    return receipt(run_id)
