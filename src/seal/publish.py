from datetime import datetime, timezone

from .core import Objects, SealError, digest
from .db import connect, decision, fenced, j, locked_run, one


def finish_run(run_id, epoch=None):
    from .runs import receipt

    with connect() as c:
        source, run = locked_run(c, run_id)
        epoch = run["attempt_epoch"] if epoch is None else epoch
        if run["status"] in ("complete", "partial", "failed", "superseded"):
            return receipt(run_id)
        if run["status"] != "finishing":
            raise SealError("crawl_not_completed")
        try:
            fenced(source, run, epoch)
        except SealError:
            c.execute(
                "UPDATE seal_run SET status='superseded',completed_at=now() WHERE id=%s", (run_id,)
            )
            return {"run_id": run_id, "status": "superseded"}
        errors = list(run["errors"])
        binding = one(c, "SELECT * FROM seal_binding WHERE id=%s", (run["binding_id"],))
        results = c.execute(
            "SELECT * FROM seal_result WHERE id=ANY(%s) ORDER BY document_id", (run["result_ids"],)
        ).fetchall()
        expected = binding["config"].get("expected_urls")
        urls = {r["candidate"]["url"] for r in results}
        # Recheck is a predeclared complete business slice: its fixed seeds.
        if run["mode"] == "recheck":
            expected = [s["url"] for s in run["seeds"]]
        coverage = {
            "status": "known" if expected is not None else "unknown",
            "denominator": len(set(expected)) if expected is not None else None,
            "observed": len(urls),
            "scope": binding["config"]["scope"],
        }
        if expected is not None and urls != set(expected):
            errors.append("expected_document_set_mismatch")
        if not results:
            errors.append("unexpected_empty_result")
        if expected is None and run["mode"] == "production":
            prior = c.execute(
                "SELECT report FROM seal_run WHERE source_id=%s AND mode='production' AND status='complete' ORDER BY completed_at DESC LIMIT 1",
                (source["id"],),
            ).fetchone()
            if prior and len(results) < prior["report"].get("result_count", 0) * 0.5:
                errors.append("unexpected_yield_drop")
        if not set(run["refs"]) <= urls:
            errors.append("unresolved_document_refs")
        used_seeds = {(i["logical_url"], i["role"]) for i in run["inputs"]}
        if not all((s["url"], s["role"]) in used_seeds for s in run["seeds"]):
            errors.append("unresolved_seed")
        production = run["mode"] in ("production", "recheck")
        if run["mode"] != "replay":
            latest_exchanges = c.execute(
                "SELECT DISTINCT ON (chain_id) status,error,final_url FROM seal_fetch_observation WHERE run_id=%s AND attempt_epoch=%s ORDER BY chain_id,requested_at DESC",
                (run_id, epoch),
            ).fetchall()
            for exchange in latest_exchanges:
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
        if production and source["needs_repair"]:
            errors.append("source_needs_repair")
        if run["report"].get("finish_reason") != "finished":
            errors.append("incomplete_crawl")
        try:
            for entry in run["inputs"]:
                snapshot = Objects().json(entry["snapshot_id"])
                Objects().get(snapshot["body_hash"])
            for result in results:
                if result["withdrawn"]:
                    errors.append("withdrawn_result")
                doc = one(
                    c,
                    "SELECT * FROM seal_document WHERE id=%s FOR UPDATE",
                    (result["document_id"],),
                )
                one(c, "SELECT * FROM seal_revision WHERE id=%s", (doc["latest_revision"],))
                latest = one(
                    c,
                    "SELECT * FROM seal_fetch_observation WHERE id=%s",
                    (doc["latest_observation"],),
                )
                if latest["snapshot_id"] not in result["inputs"]:
                    errors.append("stale_result_input")
                if production and (latest["run_id"] != run_id or latest["attempt_epoch"] != epoch):
                    errors.append("stale_result_observation")
        except SealError as exc:
            errors.append(exc.code)
        report = dict(
            run["report"], coverage=coverage, result_count=len(results), errors=sorted(set(errors))
        )
        if errors:
            structural = {
                "expected_document_set_mismatch",
                "unexpected_empty_result",
                "unresolved_document_refs",
                "unresolved_seed",
                "incomplete_crawl",
            }
            temporary = {"download_failed", "http_5xx", "source_rate_limited"}
            transient = bool(set(errors) & temporary) and not (set(errors) - structural - temporary)
            rate_limited = "source_rate_limited" in errors
            status = (
                "retryable"
                if production and transient and not rate_limited and epoch < run["max_attempts"]
                else "partial"
            )
            if production and transient and not rate_limited and epoch >= run["max_attempts"]:
                status = "failed"
            if production and not transient:
                c.execute("UPDATE seal_source SET needs_repair=true WHERE id=%s", (source["id"],))
            c.execute(
                "UPDATE seal_run SET status=%s,report=%s,errors=%s,completed_at=now() WHERE id=%s",
                (status, j(report), j(sorted(set(errors))), run_id),
            )
        else:
            if production:
                for result in results:
                    doc = one(
                        c, "SELECT * FROM seal_document WHERE id=%s", (result["document_id"],)
                    )
                    key = digest(
                        [
                            source["id"],
                            doc["id"],
                            doc["latest_revision"],
                            result["id"],
                            run["generation"],
                        ]
                    )
                    event = decision(
                        c,
                        source["id"],
                        "publish",
                        {
                            "document_id": doc["id"],
                            "revision_id": doc["latest_revision"],
                            "result_id": result["id"],
                            "generation": run["generation"],
                            "run_id": run_id,
                            "coverage": coverage,
                        },
                        "publish:" + key,
                    )
                    c.execute(
                        "UPDATE seal_document SET current_result=%s,publication_id=%s WHERE id=%s",
                        (result["id"], event, doc["id"]),
                    )
            c.execute(
                "UPDATE seal_run SET status='complete',report=%s,completed_at=now() WHERE id=%s",
                (j(report), run_id),
            )
    return receipt(run_id)


def export_source(source_id):
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        source = one(c, "SELECT * FROM seal_source WHERE id=%s", (source_id,))
        latest_run = c.execute(
            "SELECT id,status,completed_at FROM seal_run WHERE source_id=%s AND mode IN ('production','recheck') AND status IN ('complete','partial','retryable','failed') ORDER BY run_seq DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        degraded = latest_run is not None and latest_run["status"] != "complete"
        rows = c.execute(
            """SELECT d.*, r.candidate, r.inputs, r.binding_id, b.recipe_version, p.payload AS publication,
                 o.fetched_at AS observed_at, p.created_at AS published_at
          FROM seal_document d JOIN seal_result r ON r.id=d.current_result AND NOT r.withdrawn
          JOIN seal_binding b ON b.id=r.binding_id JOIN seal_decision p ON p.id=d.publication_id
          JOIN seal_fetch_observation o ON o.id=d.latest_observation
          WHERE d.source_id=%s AND d.namespace='production' ORDER BY d.identity""",
            (source_id,),
        ).fetchall()
        documents, unavailable = [], []
        for row in rows:
            try:
                for key in row["inputs"]:
                    snapshot = Objects().json(key)
                    Objects().get(snapshot["body_hash"])
            except SealError as exc:
                unavailable.append({"document_id": row["id"], "reason": exc.code})
                continue
            revision = one(
                c, "SELECT * FROM seal_revision WHERE id=%s", (row["publication"]["revision_id"],)
            )
            original = one(
                c,
                "SELECT fetched_at FROM seal_fetch_observation WHERE id=%s",
                (revision["observation_id"],),
            )
            documents.append(
                {
                    **row["candidate"],
                    "source_id": source_id,
                    "document_id": row["id"],
                    "revision_id": revision["id"],
                    "result_id": row["current_result"],
                    "binding_id": row["binding_id"],
                    "recipe_version": row["recipe_version"],
                    "activation_generation": row["publication"]["generation"],
                    "publication_id": row["publication_id"],
                    "body_hash": revision["body_hash"],
                    "snapshot_ids": row["inputs"],
                    "fetched_at": original["fetched_at"],
                    "last_observed_at": row["observed_at"],
                    "stale": degraded
                    or row["latest_revision"] != revision["id"]
                    or source["needs_repair"]
                    or source["paused"]
                    or row["next_check"] < datetime.now(timezone.utc),
                    "coverage": row["publication"]["coverage"],
                }
            )
        withdrawals = c.execute(
            "SELECT id,payload,created_at FROM seal_decision WHERE source_id=%s AND kind='withdraw' ORDER BY created_at",
            (source_id,),
        ).fetchall()
    return {
        "source_id": source_id,
        "generation": source["generation"],
        "paused": source["paused"],
        "needs_repair": source["needs_repair"],
        "documents": documents,
        "latest_run": latest_run,
        "withdrawals": withdrawals,
        "unavailable": unavailable,
    }
