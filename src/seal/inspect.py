from .core import SealError
from .db import connect, one


def inspect_record(kind, identity):
    with connect() as c:
        if kind == "binding":
            return one(c, "SELECT * FROM seal_binding WHERE id=%s", (identity,))
        if kind == "run":
            run = one(c, "SELECT * FROM seal_run WHERE id=%s", (identity,))
            return {
                "run": run,
                "observations": c.execute(
                    "SELECT * FROM seal_fetch_observation WHERE run_id=%s ORDER BY archived_at",
                    (identity,),
                ).fetchall(),
                "results": c.execute(
                    "SELECT * FROM seal_result WHERE id=ANY(%s) ORDER BY id", (run["result_ids"],)
                ).fetchall(),
            }
        if kind == "source":
            source = one(c, "SELECT * FROM seal_source WHERE id=%s", (identity,))
            return {
                "source": source,
                "documents": c.execute(
                    "SELECT * FROM seal_document WHERE source_id=%s AND namespace='production' ORDER BY identity",
                    (identity,),
                ).fetchall(),
                "revisions": c.execute(
                    "SELECT r.* FROM seal_revision r JOIN seal_document d ON r.document_id=d.id WHERE d.source_id=%s AND d.namespace='production' ORDER BY r.created_at",
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
    raise SealError("unknown_inspection_kind")
