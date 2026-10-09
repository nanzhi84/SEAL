"""Frozen target plans and completion-owned Recheck dates, under the Source lock.

Request budgets apply to unique frozen GETs. A business identity follows its
proven parent even if it also exposes a navigational detail URL. Failed/missing
targets retain their due date; separate bounded retry dates prevent starvation.
"""

from .core import Objects, SealError, public_url
from .record_validation import ParentRequest


def _historical_parent(c, source_id, record):
    """Recover only a unique primary GET from a successful accepted emission."""
    candidate = record["candidate"]
    snapshot_id = candidate.get("primary_snapshot_id")
    if not snapshot_id:
        raise SealError("record_recheck_request_missing")
    rows = c.execute(
        """SELECT DISTINCT run.inputs FROM seal_record_emission e
           JOIN seal_run run ON run.id=e.run_id
           WHERE e.record_id=%s AND e.result_id=%s AND run.source_id=%s
             AND run.namespace='runtime' AND run.status='complete'""",
        (record["id"], record["latest_result"], source_id),
    ).fetchall()
    candidates = {}
    for row in rows:
        for entry in row["inputs"]:
            if entry.get("snapshot_id") != snapshot_id or entry.get("method") != "GET":
                continue
            request = ParentRequest.model_validate(
                {
                    "url": public_url(entry["logical_url"]),
                    "role": entry["role"],
                    "method": "GET",
                }
            ).model_dump()
            candidates[(request["url"], request["role"])] = request
    if len(candidates) != 1:
        raise SealError("record_recheck_request_missing")
    snapshot = Objects().json(snapshot_id)
    Objects().get(snapshot["body_hash"])
    if snapshot.get("method") != "GET" or snapshot.get("status") != 200:
        raise SealError("record_recheck_request_missing")
    return next(iter(candidates.values()))


def _record_request(c, source_id, record):
    candidate = record["candidate"]
    if candidate.get("key_locator", {}).get("kind") == "response_url":
        if not record["detail_url"]:
            raise SealError("record_recheck_request_missing")
        return {"url": public_url(record["detail_url"]), "role": "detail", "method": "GET"}
    parent = record["parent_request"]
    if parent is None:
        parent = _historical_parent(c, source_id, record)
    parent = ParentRequest.model_validate(parent).model_dump()
    parent["url"] = public_url(parent["url"])
    return parent


def plan_recheck(c, source, config, due_only=False):
    """Freeze explicit eligible targets; never change next_check while planning."""
    due = (
        "AND next_check<=now() AND (recheck_retry_at IS NULL OR recheck_retry_at<=now())"
        if due_only
        else ""
    )
    documents = (
        c.execute(
            "SELECT id,url,next_check FROM seal_document d WHERE source_id=%s "
            "AND namespace='runtime' AND EXISTS (SELECT 1 FROM seal_result r WHERE r.document_id=d.id) "
            + due
            + " ORDER BY next_check,identity",
            (source["id"],),
        ).fetchall()
        if config["output_schema"] == "generic_document.v1"
        else []
    )
    records = (
        c.execute(
            "SELECT r.*,jsonb_build_object('key_locator',rr.candidate->'key_locator',"
            "'primary_snapshot_id',rr.candidate->'primary_snapshot_id') AS candidate "
            "FROM seal_record r JOIN seal_record_result rr ON rr.id=r.latest_result "
            "WHERE r.source_id=%s AND r.namespace='runtime' "
            + due
            + " ORDER BY next_check,record_type,record_key",
            (source["id"],),
        ).fetchall()
        if config["output_schema"] == "record.v1"
        else []
    )
    targets = [(d["next_check"], "document", d["id"], d) for d in documents] + [
        (r["next_check"], "record", r["record_type"] + ":" + r["record_key"], r) for r in records
    ]
    targets.sort(key=lambda value: value[:3])
    groups, selected = {}, {"records": [], "documents": []}
    request_budget = config["budget"]["requests"]
    for _, kind, _, target in targets:
        request = (
            _record_request(c, source["id"], target)
            if kind == "record"
            else {"url": public_url(target["url"]), "role": "detail", "method": "GET"}
        )
        key = (request["url"], request["method"])
        if key not in groups and len(groups) >= request_budget:
            if due_only:
                continue
            raise SealError("recheck_budget_exceeded")
        if key in groups and groups[key]["role"] != request["role"]:
            raise SealError("record_recheck_role_conflict")
        groups.setdefault(key, request)
        if kind == "record":
            selected["records"].append(
                {
                    "record_id": target["id"],
                    "record_type": target["record_type"],
                    "record_key": target["record_key"],
                }
            )
        else:
            selected["documents"].append({"document_id": target["id"], "url": target["url"]})
    if not groups:
        raise SealError("no_documents_to_recheck")
    return list(groups.values()), {
        "contract": 1,
        "selection": "due" if due_only else "manual_all",
        "request_budget": request_budget,
        "unique_request_count": len(groups),
        "eligible_record_count": len(records),
        "eligible_document_count": len(documents),
        **selected,
    }


def unobserved_records(c, source, run):
    plan = run.get("recheck_plan")
    if plan is not None:
        observed = {
            (row["record_type"], row["record_key"])
            for row in c.execute(
                """SELECT DISTINCT r.record_type,r.record_key FROM seal_record_emission e
                   JOIN seal_record r ON r.id=e.record_id
                   WHERE e.run_id=%s AND e.attempt_epoch=%s""",
                (run["id"], run["attempt_epoch"]),
            ).fetchall()
        }
        return [
            target
            for target in plan["records"]
            if (target["record_type"], target["record_key"]) not in observed
        ]
    return c.execute(
        """SELECT r.id AS record_id,r.record_type,r.record_key FROM seal_record r
           WHERE r.source_id=%s AND r.namespace=%s AND r.latest_result IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM seal_record_emission e WHERE e.record_id=r.id
               AND e.run_id=%s AND e.attempt_epoch=%s) ORDER BY r.record_type,r.record_key""",
        (source["id"], run["namespace"], run["id"], run["attempt_epoch"]),
    ).fetchall()


def scope_details(run):
    plan = run.get("recheck_plan")
    if plan is None:
        return {"recheck_plan": None, "absence_population": "known_records_in_run_namespace"}
    return {
        "recheck_plan": plan,
        "absence_population": "frozen_planned_records_only",
        "planned_record_count": len(plan["records"]),
        "planned_document_count": len(plan["documents"]),
    }


def _advance(c, table, identities, source, run):
    if identities:
        c.execute(
            f"UPDATE {table} SET next_check=now()+(%s * interval '1 second'),"
            "recheck_retry_at=NULL,recheck_failures=0 WHERE id=ANY(%s) AND source_id=%s AND namespace=%s",
            (source["config"]["recheck_seconds"], list(identities), source["id"], run["namespace"]),
        )


def _backoff(c, table, identities, source, run):
    if identities:
        c.execute(
            f"UPDATE {table} SET recheck_retry_at=now()+(LEAST(3600,30 * "
            "power(2,LEAST(recheck_failures,7))) * interval '1 second'),"
            "recheck_failures=LEAST(recheck_failures+1,1000) "
            "WHERE id=ANY(%s) AND source_id=%s AND namespace=%s",
            (list(identities), source["id"], run["namespace"]),
        )


def complete_dates(c, source, run, record_outputs, document_outputs):
    """Only successful observations of planned targets count as completed checks."""
    records = {item["record_id"] for item in record_outputs}
    documents = {
        row["document_id"]
        for row in c.execute(
            "SELECT document_id FROM seal_result WHERE id=ANY(%s)",
            ([item["result_id"] for item in document_outputs],),
        ).fetchall()
    }
    plan = run.get("recheck_plan") if run["mode"] == "recheck" else None
    if plan is not None:
        planned_records = {item["record_id"] for item in plan["records"]}
        planned_documents = {item["document_id"] for item in plan["documents"]}
        _backoff(c, "seal_record", planned_records - records, source, run)
        _backoff(c, "seal_document", planned_documents - documents, source, run)
        records &= planned_records
        documents &= planned_documents
    _advance(c, "seal_record", records, source, run)
    _advance(c, "seal_document", documents, source, run)


def failed_dates(c, source, run, status):
    plan = run.get("recheck_plan")
    if (
        run["mode"] != "recheck"
        or plan is None
        or status not in ("partial", "failed")
        or source["generation"] != run["generation"]
        or source["paused"]
        or source["write_seq"] > run["run_seq"]
    ):
        return
    _backoff(c, "seal_record", {item["record_id"] for item in plan["records"]}, source, run)
    _backoff(c, "seal_document", {item["document_id"] for item in plan["documents"]}, source, run)


def has_due(c, source_id, output_schema):
    table, predicate = (
        ("seal_record", "latest_result IS NOT NULL")
        if output_schema == "record.v1"
        else ("seal_document", "EXISTS (SELECT 1 FROM seal_result r WHERE r.document_id=t.id)")
    )
    return (
        c.execute(
            f"SELECT 1 FROM {table} t WHERE source_id=%s AND namespace='runtime' "
            "AND next_check<=now() AND (recheck_retry_at IS NULL OR recheck_retry_at<=now()) "
            f"AND {predicate} LIMIT 1",
            (source_id,),
        ).fetchone()
        is not None
    )
