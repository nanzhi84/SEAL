"""Read execution evidence, never a reviewed or published business dataset."""

from datetime import datetime, timezone

from .core import Objects, SealError
from .db import connect, one
from .manifest import manifest_reference


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
        records, unavailable_records = export_records(c, source, run_id, latest_run)
    return {
        "source_id": source_id,
        "generation": source["generation"],
        "paused": source["paused"],
        "quality_status": "not_evaluated",
        "documents": documents,
        "records": records,
        "latest_run": latest_run,
        "run": (
            {
                "id": selected_run["id"],
                "status": selected_run["status"],
                "errors": selected_run["errors"],
                "report": selected_run["report"],
                **manifest_reference(selected_run),
            }
            if selected_run
            else None
        ),
        "unavailable": unavailable,
        "unavailable_records": unavailable_records,
    }


def export_records(connection, source, run_id, latest_run):
    """Latest complete online results, or valid nonconflicting outputs of one Run."""
    from .manifest import has_table

    if not has_table(connection, "seal_record"):
        return [], []
    latest_report = {}
    if latest_run:
        latest_report = one(
            connection, "SELECT report FROM seal_run WHERE id=%s", (latest_run["id"],)
        )["report"]
    latest_observed = {entry["record_id"] for entry in latest_report.get("record_outputs", [])}
    latest_unobserved = {
        entry["record_id"]
        for entry in latest_report.get("scope_evidence", {}).get("unobserved_records", [])
    }
    latest_conflicts = set(latest_report.get("record_counts", {}).get("conflicted_record_ids", []))
    rows = connection.execute(
        """WITH selected AS (
          SELECT DISTINCT ON (result.record_id)
            run.id AS run_id,run.mode,run.status AS run_status,run.generation,
            run.attempt_epoch AS run_attempt_epoch,
            output AS frozen_output,result.*
          FROM seal_run run
          CROSS JOIN LATERAL jsonb_array_elements(
            COALESCE(run.report->'record_outputs','[]')) output
          JOIN seal_record_result result ON result.id=output->>'record_result_id'
          WHERE run.source_id=%s AND
            (run.id=%s OR (%s::text IS NULL AND run.mode IN ('collect','recheck') AND run.status='complete'))
          ORDER BY result.record_id,run.run_seq DESC,run.created_at DESC,run.id
        ) SELECT s.*,b.recipe_version,v.created_at AS version_created_at,
            v.content_hash,v.change_reason,r.latest_version,r.next_check
          FROM selected s JOIN seal_binding b ON b.id=s.binding_id
          JOIN seal_record_version v ON v.id=s.frozen_output->>'record_version_id'
            AND v.record_id=s.record_id
          JOIN seal_record r ON r.id=s.record_id
          ORDER BY r.record_type,r.record_key""",
        (source["id"], run_id, run_id),
    ).fetchall()
    evidence_ids = {
        identity
        for row in rows
        for identity in row["frozen_output"].get("record_result_ids", [row["id"]])
    }
    evidence = {
        row["id"]: row
        for row in connection.execute(
            "SELECT * FROM seal_record_result WHERE id=ANY(%s)", (list(evidence_ids),)
        ).fetchall()
    }
    observation_ids = {
        entry["observation_id"] for row in rows for entry in row["frozen_output"]["inputs"]
    }
    observations = {
        row["id"]: row
        for row in connection.execute(
            "SELECT id,fetched_at,origin FROM seal_fetch_observation WHERE id=ANY(%s)",
            (list(observation_ids),),
        ).fetchall()
    }
    primary_inputs = {}
    for emission in connection.execute(
        """SELECT e.run_id,e.attempt_epoch,e.result_id,e.observation_id,o.snapshot_id
           FROM seal_record_emission e JOIN seal_fetch_observation o ON o.id=e.observation_id
           WHERE e.run_id=ANY(%s) AND e.result_id=ANY(%s)
           ORDER BY e.created_at,e.id""",
        (list({row["run_id"] for row in rows}), list(evidence_ids)),
    ).fetchall():
        key = (emission["run_id"], emission["attempt_epoch"], emission["result_id"])
        entry = {
            "observation_id": emission["observation_id"],
            "snapshot_id": emission["snapshot_id"],
        }
        if entry not in primary_inputs.setdefault(key, []):
            primary_inputs[key].append(entry)
    snapshots, records, unavailable = {}, [], []
    for row in rows:
        frozen = row["frozen_output"]
        try:
            raw_refs = []
            for entry in frozen["inputs"]:
                key = entry["snapshot_id"]
                if key not in snapshots:
                    snapshot = Objects().json(key)
                    Objects().get(snapshot["body_hash"])
                    snapshots[key] = snapshot
                observed = observations.get(entry["observation_id"])
                if observed is None:
                    raise SealError("record_observation_missing")
                raw_refs.append(
                    dict(
                        entry,
                        body_hash=snapshots[key]["body_hash"],
                        body_size=snapshots[key]["body_size"],
                        fetched_at=observed["fetched_at"],
                        origin=observed["origin"],
                    )
                )
            result_evidence = []
            for identity in frozen.get("record_result_ids", [row["id"]]):
                item = evidence.get(identity)
                if item is None or item["record_id"] != row["record_id"]:
                    raise SealError("record_result_reference_missing")
                result_evidence.append(
                    {
                        "record_result_id": identity,
                        "binding_id": item["binding_id"],
                        "snapshot_ids": item["inputs"],
                        "primary_inputs": primary_inputs.get(
                            (row["run_id"], row["run_attempt_epoch"], identity), []
                        ),
                        "primary_snapshot_id": item["candidate"].get("primary_snapshot_id"),
                        "locators": item["candidate"]["locators"],
                        "key_locator": item["candidate"]["key_locator"],
                    }
                )
        except SealError as exc:
            unavailable.append(
                {"record_id": row["record_id"], "record_result_id": row["id"], "reason": exc.code}
            )
            continue
        records.append(
            {
                **row["candidate"],
                "source_id": source["id"],
                "record_id": row["record_id"],
                "record_version_id": frozen["record_version_id"],
                "record_result_id": row["id"],
                "record_result_ids": frozen.get("record_result_ids", [row["id"]]),
                "observation_id": frozen["observation_id"],
                "inputs": raw_refs,
                "resource_inputs": frozen.get("resource_inputs", []),
                "snapshot_ids": sorted({entry["snapshot_id"] for entry in raw_refs}),
                "result_evidence": result_evidence,
                "run_id": row["run_id"],
                "run_status": row["run_status"],
                "mode": row["mode"],
                "binding_id": row["binding_id"],
                "recipe_version": row["recipe_version"],
                "source_generation": row["generation"],
                "content_hash": row["content_hash"],
                "content_change": frozen["content_change"],
                "raw_changed": frozen["raw_changed"],
                "input_set_changed": frozen.get("input_set_changed"),
                "reprocessed": frozen.get("reprocessed", False),
                "latest_run_observation": (
                    "conflicted"
                    if row["record_id"] in latest_conflicts
                    else "observed"
                    if row["record_id"] in latest_observed
                    else "unobserved"
                    if row["record_id"] in latest_unobserved
                    else "unknown"
                ),
                "version_created_at": row["version_created_at"],
                "last_observed_at": observations[frozen["observation_id"]]["fetched_at"],
                "quality_status": "not_evaluated",
                "stale": row["latest_version"] != frozen["record_version_id"]
                or (
                    not run_id
                    and (
                        source["paused"]
                        or row["record_id"] in latest_unobserved
                        or row["next_check"] < datetime.now(timezone.utc)
                        or (latest_run is not None and latest_run["status"] != "complete")
                    )
                ),
            }
        )
    return records, unavailable
