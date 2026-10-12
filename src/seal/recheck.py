"""Online frozen document checks; stateful POST sources refresh from their seeds."""

from .core import SealError


def plan_recheck(c, source, config):
    documents = c.execute(
        """SELECT d.id,d.url,d.identity,COALESCE(input->>'role','detail') AS role
        FROM seal.document d JOIN seal.document_version v ON v.id=d.latest_document_version
        LEFT JOIN LATERAL (SELECT value AS input FROM seal.run r,
          LATERAL jsonb_array_elements(r.inputs) WHERE r.source_id=d.source_id
          AND value->>'snapshot_id'=v.snapshot_id ORDER BY r.created_at DESC LIMIT 1) actual ON true
        WHERE d.source_id=%s AND d.namespace='runtime' ORDER BY d.next_check,d.identity""",
        (source["id"],),
    ).fetchall()
    if not documents:
        raise SealError("no_documents_to_recheck")
    stateful = "POST" in config["methods"]
    seeds = (
        [{"url": url, "role": config["seed_role"]} for url in config["entry_urls"]]
        if stateful
        else [{"url": d["url"], "role": d["role"]} for d in documents]
    )
    if len(seeds) > config["budget"]["requests"]:
        raise SealError("recheck_budget_exceeded")
    return seeds, {
        "contract": 2,
        "selection": "source_seeds" if stateful else "saved_documents",
        "documents": [{"document_id": d["id"], "url": d["url"]} for d in documents],
        "unique_request_count": len(seeds),
    }


def scope_details(run):
    return {
        "recheck_plan": run.get("recheck_plan"),
        "absence_semantics": "unobserved_is_unknown_never_deleted",
    }


def complete_dates(c, source, run, outputs):
    documents = {item["document_id"] for item in outputs}
    c.execute(
        "UPDATE seal.document SET next_check=now()+(%s * interval '1 second'),recheck_retry_at=NULL,recheck_failures=0 WHERE id=ANY(%s) AND source_id=%s",
        (source["config"]["recheck_seconds"], list(documents), source["id"]),
    )


def failed_dates(c, source, run, status):
    plan = run.get("recheck_plan")
    if (
        run["mode"] != "recheck"
        or not plan
        or status != "failed"
        or source["generation"] != run["generation"]
        or source["paused"]
        or source["write_seq"] > run["run_seq"]
    ):
        return
    c.execute(
        "UPDATE seal.document SET recheck_retry_at=now()+(LEAST(3600,30 * power(2,LEAST(recheck_failures,7))) * interval '1 second'),recheck_failures=LEAST(recheck_failures+1,1000) WHERE id=ANY(%s) AND source_id=%s",
        ([d["document_id"] for d in plan["documents"]], source["id"]),
    )
