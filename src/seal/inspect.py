"""Read Runtime evidence and immutable manifest mappings without changing state."""

from .core import SealError
from .db import connect, one
from .manifest import has_table, manifest_reference, read_manifest


def run_evidence(connection, run):
    value = {
        "run": run,
        "observations": connection.execute(
            "SELECT * FROM seal_fetch_observation WHERE run_id=%s ORDER BY archived_at",
            (run["id"],),
        ).fetchall(),
        "results": connection.execute(
            "SELECT * FROM seal_result WHERE id=ANY(%s) ORDER BY id", (run["result_ids"],)
        ).fetchall(),
        **manifest_reference(run),
    }
    if has_table(connection, "seal_record"):
        value["record_results"] = connection.execute(
            "SELECT DISTINCT r.* FROM seal_record_result r JOIN seal_record_emission e ON e.result_id=r.id WHERE e.run_id=%s ORDER BY r.created_at,r.id",
            (run["id"],),
        ).fetchall()
        value["record_versions"] = connection.execute(
            "SELECT * FROM seal_record_version WHERE run_id=%s OR id=ANY(%s) ORDER BY created_at,id",
            (
                run["id"],
                [output["record_version_id"] for output in run["report"].get("record_outputs", [])],
            ),
        ).fetchall()
        value["record_emissions"] = connection.execute(
            "SELECT * FROM seal_record_emission WHERE run_id=%s ORDER BY created_at,id",
            (run["id"],),
        ).fetchall()
    if has_table(connection, "seal_discovery"):
        from .discovery import summarize_discovery

        value["discovery"] = summarize_discovery(connection, run, include_events=True)
    return value


def inspect_record(kind, identity):
    if kind == "manifest":
        return {"manifest_id": identity, "manifest": read_manifest(identity)}
    with connect() as c:
        if kind == "binding":
            return one(c, "SELECT * FROM seal_binding WHERE id=%s", (identity,))
        if kind == "run":
            return run_evidence(c, one(c, "SELECT * FROM seal_run WHERE id=%s", (identity,)))
        if kind == "research":
            runs = c.execute(
                "SELECT r.* FROM seal_run r JOIN seal_binding b ON b.id=r.binding_id WHERE b.config->'research_ids' ? %s ORDER BY r.created_at,r.id",
                (identity,),
            ).fetchall()
            return {
                "research_id": identity,
                "quality_status": "not_evaluated",
                "runs": [
                    {
                        "source_id": run["source_id"],
                        "binding_id": run["binding_id"],
                        "run_id": run["id"],
                        "status": run["status"],
                        "errors": run["errors"],
                        **manifest_reference(run),
                    }
                    for run in runs
                ],
            }
        if kind == "source":
            source = one(c, "SELECT * FROM seal_source WHERE id=%s", (identity,))
            value = {
                "source": source,
                "documents": c.execute(
                    "SELECT * FROM seal_document WHERE source_id=%s ORDER BY identity",
                    (identity,),
                ).fetchall(),
                "revisions": c.execute(
                    "SELECT r.* FROM seal_revision r JOIN seal_document d ON r.document_id=d.id WHERE d.source_id=%s ORDER BY r.created_at",
                    (identity,),
                ).fetchall(),
                "decisions": c.execute(
                    "SELECT * FROM seal_decision WHERE source_id=%s ORDER BY created_at",
                    (identity,),
                ).fetchall(),
                "runs": c.execute(
                    "SELECT id,mode,status,attempt_epoch,job_id,report FROM seal_run WHERE source_id=%s ORDER BY created_at",
                    (identity,),
                ).fetchall(),
            }
            if has_table(c, "seal_record"):
                value["records"] = c.execute(
                    "SELECT * FROM seal_record WHERE source_id=%s ORDER BY namespace,record_type,record_key",
                    (identity,),
                ).fetchall()
                value["record_versions"] = c.execute(
                    "SELECT v.* FROM seal_record_version v JOIN seal_record r ON r.id=v.record_id WHERE r.source_id=%s ORDER BY v.created_at,v.id",
                    (identity,),
                ).fetchall()
            return value
    raise SealError("unknown_inspection_kind")
