"""Technical completion only: no quality assessment or publication decisions."""

from collections import Counter

from .core import Objects, SealError
from .db import connect, fenced, j, locked_run, one
from .manifest import has_table, terminal_report, write_manifest


def completion_status(errors, online, epoch, max_attempts):
    if not errors:
        return "complete"
    structural = {"unresolved_document_refs", "unresolved_seed", "incomplete_crawl"}
    temporary = {"download_failed", "http_5xx", "source_rate_limited"}
    transient = bool(set(errors) & temporary) and not (set(errors) - structural - temporary)
    if online and transient and "source_rate_limited" not in errors:
        return "retryable" if epoch < max_attempts else "failed"
    return "partial"


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
        except SealError as exc:
            report = terminal_report(c, source, run, "superseded", exc.code)
            c.execute(
                "UPDATE seal_run SET status='superseded',report=%s,errors=%s,completed_at=now() WHERE id=%s",
                (j(report), j(report["errors"]), run_id),
            )
            return {
                "run_id": run_id,
                "status": "superseded",
                "attempt_epoch": epoch,
                "errors": report["errors"],
                "report": report,
            }
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
            policy_column = (
                "EXISTS (SELECT 1 FROM seal_discovery d WHERE d.run_id=o.run_id "
                "AND d.attempt_epoch=o.attempt_epoch AND d.chain_id=o.chain_id "
                "AND (d.role='robots' OR d.reason='optional_entry_missing')) AS absent_entry_allowed"
                if has_table(c, "seal_discovery")
                else "false AS absent_entry_allowed"
            )
            exchanges = c.execute(
                "SELECT DISTINCT ON (o.chain_id) o.status,o.error,o.final_url,"
                + policy_column
                + " FROM seal_fetch_observation o WHERE o.run_id=%s AND o.attempt_epoch=%s "
                "ORDER BY o.chain_id,o.requested_at DESC",
                (run_id, epoch),
            ).fetchall()
            for exchange in exchanges:
                status = exchange["status"]
                if exchange["absent_entry_allowed"] and status in {404, 410}:
                    continue
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
        input_artifacts = []
        for entry in run["inputs"]:
            try:
                snapshot = Objects().json(entry["snapshot_id"])
                Objects().get(snapshot["body_hash"])
                input_artifacts.append(
                    dict(entry, body_hash=snapshot["body_hash"], body_size=snapshot["body_size"])
                )
            except SealError as exc:
                errors.append(exc.code)
                input_artifacts.append(dict(entry, availability="unavailable", reason=exc.code))
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
        record_finish = {"outputs": [], "errors": [], "counts": {}}
        records_available = has_table(c, "seal_record")
        if records_available:
            from .records import finish_records

            record_finish = finish_records(c, source, run)
            errors.extend(record_finish["errors"])
        discovery = {"contract": 1, "availability": "not_recorded", "unknown_coverage": True}
        if has_table(c, "seal_discovery"):
            from .discovery import finish_discovery, summarize_discovery

            finish_discovery(c, run, run["report"].get("finish_reason", "unknown"))
            discovery = summarize_discovery(c, run)
            errors.extend(discovery.get("unknown_coverage_reasons", []))
        from .recheck import complete_dates, failed_dates, scope_details

        unobserved = []
        if records_available:
            from .recheck import unobserved_records

            unobserved = unobserved_records(c, source, run)
        errors = sorted(set(errors))
        from .discovery import network_counts

        resource_counts = network_counts(c, run)
        resource_counts.update(
            input_count=len(run["inputs"]),
            input_roles=dict(Counter(entry["role"] for entry in run["inputs"])),
        )
        scope_evidence = {
            **scope_details(run),
            "seed_count": len(run["seeds"]),
            "resolved_seed_count": sum(
                (seed["url"], seed["role"]) in used_seeds for seed in run["seeds"]
            ),
            "business_population": "unknown",
            "unknown_coverage": bool(errors) or discovery["unknown_coverage"] or bool(unobserved),
            "unknown_coverage_reasons": sorted(
                set(errors + (["records_unobserved"] if unobserved else []))
            ),
            "pagination_inputs": [
                entry["snapshot_id"] for entry in run["inputs"] if entry["role"] in ("list", "api")
            ],
            "attachment_inputs": [
                entry["snapshot_id"] for entry in run["inputs"] if entry["role"] == "attachment"
            ],
            "record_absence_semantics": "unobserved_is_unknown_never_deleted",
            "unobserved_records": unobserved,
            "unobserved_record_count": len(unobserved),
        }
        report = dict(
            run["report"],
            result_count=len(results),
            outputs=outputs,
            record_outputs=record_finish["outputs"],
            record_counts=record_finish["counts"],
            discovery=discovery,
            resource_counts=resource_counts,
            scope_evidence=scope_evidence,
            quality_status="not_evaluated",
            errors=errors,
        )
        status = completion_status(errors, online, epoch, run["max_attempts"])
        binding = one(c, "SELECT * FROM seal_binding WHERE id=%s", (run["binding_id"],))
        try:
            report["manifest_id"] = write_manifest(
                source, binding, run, report, status, input_artifacts
            )
        except (SealError, OSError, TypeError, ValueError) as exc:
            errors = sorted(set([*errors, "manifest_archive_failed"]))
            report.update(
                errors=errors,
                manifest_id=None,
                manifest_error=exc.code
                if isinstance(exc, SealError)
                else "manifest_archive_failed",
            )
            scope_evidence.update(
                unknown_coverage=True,
                unknown_coverage_reasons=sorted(
                    set(errors + (["records_unobserved"] if unobserved else []))
                ),
            )
            status = completion_status(errors, online, epoch, run["max_attempts"])
        if records_available and status == "complete":
            from .records import accept_records

            accept_records(c, source, run, record_finish["outputs"])
        if status == "complete":
            complete_dates(c, source, run, record_finish["outputs"], outputs)
        else:
            failed_dates(c, source, run, status)
        c.execute(
            "UPDATE seal_run SET status=%s,report=%s,errors=%s,completed_at=now() WHERE id=%s",
            (status, j(report), j(errors), run_id),
        )
    return receipt(run_id)
