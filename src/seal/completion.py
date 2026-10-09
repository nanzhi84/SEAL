"""Technical completion only: no quality assessment or publication decisions."""

from .core import Objects, SealError
from .db import connect, fenced, j, locked_run, one


def finish_run(run_id, epoch=None):
    from .runs import receipt

    with connect() as c:
        source, run = locked_run(c, run_id)
        epoch = run["attempt_epoch"] if epoch is None else epoch
        if run["status"] in ("complete", "partial", "failed", "superseded"):
            return receipt(run_id)
        if run["mode"] not in ("collect", "recheck", "replay"):
            raise SealError("unsupported_run_mode")
        if run["status"] != "finishing":
            raise SealError("crawl_not_completed")
        # A late finisher for an old attempt must not terminate a newer attempt.
        if epoch != run["attempt_epoch"]:
            raise SealError("stale_attempt")
        try:
            fenced(source, run, epoch)
        except SealError:
            c.execute(
                "UPDATE seal_run SET status='superseded',completed_at=now() WHERE id=%s", (run_id,)
            )
            return {"run_id": run_id, "status": "superseded"}
        errors = list(run["errors"])
        results = c.execute(
            "SELECT * FROM seal_result WHERE id=ANY(%s) ORDER BY document_id", (run["result_ids"],)
        ).fetchall()
        if len(results) != len(run["result_ids"]):
            errors.append("result_reference_missing")
        urls = {r["candidate"]["url"] for r in results}
        if not set(run["refs"]) <= urls:
            errors.append("unresolved_document_refs")
        used_seeds = {(i["logical_url"], i["role"]) for i in run["inputs"]}
        if not all((s["url"], s["role"]) in used_seeds for s in run["seeds"]):
            errors.append("unresolved_seed")
        online = run["mode"] != "replay"
        if online:
            exchanges = c.execute(
                "SELECT DISTINCT ON (chain_id) status,error,final_url FROM seal_fetch_observation WHERE run_id=%s AND attempt_epoch=%s ORDER BY chain_id,requested_at DESC",
                (run_id, epoch),
            ).fetchall()
            for exchange in exchanges:
                status = exchange["status"]
                if status is None:
                    errors.append("download_failed")
                elif status == 429:
                    errors.append("source_rate_limited")
                elif status >= 500:
                    errors.append("http_5xx")
                elif status >= 400:
                    errors.append("http_error")
                elif status == 304:
                    errors.append("unexpected_304")
                elif 300 <= status < 400 and exchange["final_url"] is None:
                    errors.append("redirect_incomplete")
        if run["report"].get("finish_reason") != "finished":
            errors.append("incomplete_crawl")
        for entry in run["inputs"]:
            try:
                snapshot = Objects().json(entry["snapshot_id"])
                Objects().get(snapshot["body_hash"])
            except SealError as exc:
                errors.append(exc.code)
        outputs = []
        for result in results:
            doc = one(
                c, "SELECT * FROM seal_document WHERE id=%s FOR UPDATE", (result["document_id"],)
            )
            latest = one(
                c, "SELECT * FROM seal_fetch_observation WHERE id=%s", (doc["latest_observation"],)
            )
            if latest["snapshot_id"] not in result["inputs"]:
                errors.append("stale_result_input")
                continue
            if online and (latest["run_id"] != run_id or latest["attempt_epoch"] != epoch):
                errors.append("stale_result_observation")
                continue
            outputs.append(
                {
                    "result_id": result["id"],
                    "revision_id": doc["latest_revision"],
                    "observation_id": latest["id"],
                }
            )
        errors = sorted(set(errors))
        report = dict(
            run["report"],
            result_count=len(results),
            outputs=outputs,
            quality_status="not_evaluated",
            errors=errors,
        )
        status = "complete"
        if errors:
            structural = {"unresolved_document_refs", "unresolved_seed", "incomplete_crawl"}
            temporary = {"download_failed", "http_5xx", "source_rate_limited"}
            transient = bool(set(errors) & temporary) and not (set(errors) - structural - temporary)
            status = "partial"
            if online and transient and "source_rate_limited" not in errors:
                status = "retryable" if epoch < run["max_attempts"] else "failed"
        c.execute(
            "UPDATE seal_run SET status=%s,report=%s,errors=%s,completed_at=now() WHERE id=%s",
            (status, j(report), j(errors), run_id),
        )
    return receipt(run_id)
