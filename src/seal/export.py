"""Read execution evidence, never a reviewed or published business dataset."""

from datetime import datetime, timezone

from .core import Objects, SealError
from .db import connect, one


def export_source(source_id, run_id=None):
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        source = one(c, "SELECT * FROM seal_source WHERE id=%s", (source_id,))
        selected_run = None
        if run_id:
            selected_run = one(c, "SELECT * FROM seal_run WHERE id=%s", (run_id,))
            if selected_run["source_id"] != source_id:
                raise SealError("run_source_mismatch")
            if selected_run["mode"] not in ("collect", "recheck", "replay"):
                raise SealError("historical_run_use_inspect_or_replay")
        latest_run = c.execute(
            "SELECT id,status,completed_at FROM seal_run WHERE source_id=%s AND mode IN ('collect','recheck') ORDER BY run_seq DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        rows = c.execute(
            """WITH selected AS (
              SELECT DISTINCT ON (result.document_id)
                run.id AS run_id, run.mode, run.status AS run_status, run.generation,
                output->>'revision_id' AS revision_id,
                output->>'observation_id' AS observation_id, result.*
              FROM seal_run run
              CROSS JOIN LATERAL jsonb_array_elements(COALESCE(run.report->'outputs','[]')) output
              JOIN seal_result result ON result.id=output->>'result_id'
              WHERE run.source_id=%s AND
                (run.id=%s OR (%s::text IS NULL AND run.mode IN ('collect','recheck') AND run.status='complete'))
              ORDER BY result.document_id,run.run_seq DESC,run.created_at DESC
            ) SELECT s.*, b.recipe_version, v.body_hash,
                original.fetched_at, observed.fetched_at AS last_observed_at,
                d.latest_revision, d.next_check
              FROM selected s JOIN seal_binding b ON b.id=s.binding_id
              JOIN seal_revision v ON v.id=s.revision_id
              JOIN seal_fetch_observation original ON original.id=v.observation_id
              JOIN seal_fetch_observation observed ON observed.id=s.observation_id
              JOIN seal_document d ON d.id=s.document_id
              ORDER BY d.identity""",
            (source_id, run_id, run_id),
        ).fetchall()
        documents, unavailable = [], []
        for row in rows:
            try:
                for key in row["inputs"]:
                    snapshot = Objects().json(key)
                    Objects().get(snapshot["body_hash"])
            except SealError as exc:
                unavailable.append(
                    {"document_id": row["document_id"], "result_id": row["id"], "reason": exc.code}
                )
                continue
            documents.append(
                {
                    **row["candidate"],
                    "source_id": source_id,
                    "document_id": row["document_id"],
                    "revision_id": row["revision_id"],
                    "observation_id": row["observation_id"],
                    "result_id": row["id"],
                    "run_id": row["run_id"],
                    "run_status": row["run_status"],
                    "mode": row["mode"],
                    "binding_id": row["binding_id"],
                    "recipe_version": row["recipe_version"],
                    "source_generation": row["generation"],
                    "body_hash": row["body_hash"],
                    "snapshot_ids": row["inputs"],
                    "fetched_at": row["fetched_at"],
                    "last_observed_at": row["last_observed_at"],
                    "quality_status": "not_evaluated",
                    "stale": row["latest_revision"] != row["revision_id"]
                    or (
                        not run_id
                        and (
                            source["paused"]
                            or row["next_check"] < datetime.now(timezone.utc)
                            or (latest_run is not None and latest_run["status"] != "complete")
                        )
                    ),
                }
            )
    return {
        "source_id": source_id,
        "generation": source["generation"],
        "paused": source["paused"],
        "quality_status": "not_evaluated",
        "documents": documents,
        "latest_run": latest_run,
        "run": (
            {
                "id": selected_run["id"],
                "status": selected_run["status"],
                "errors": selected_run["errors"],
                "report": selected_run["report"],
            }
            if selected_run
            else None
        ),
        "unavailable": unavailable,
    }
