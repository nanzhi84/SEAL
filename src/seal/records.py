"""Immutable business Record emissions, versions and current-pointer persistence."""

from collections import defaultdict

from .core import Objects, SealError, digest, public_url, uid
from .db import connect, fenced, j, locked_run, one
from .record_validation import validate_record


def _stage_resource(c, source, run, entry):
    """Freeze this response's raw URL/revision association in the Run inputs."""
    known = next(
        (
            value
            for value in run["inputs"]
            if value["snapshot_id"] == entry["snapshot_id"]
            and value["observation_id"] == entry["observation_id"]
        ),
        None,
    )
    if known and known.get("resource_input"):
        return known["resource_input"]
    snapshot = Objects().json(entry["snapshot_id"])
    if snapshot["method"] != "GET" or snapshot["status"] != 200:
        return None
    observation = one(
        c, "SELECT * FROM seal_fetch_observation WHERE id=%s", (entry["observation_id"],)
    )
    if (
        observation["source_id"] != source["id"]
        or observation["snapshot_id"] != entry["snapshot_id"]
    ):
        raise SealError("resource_observation_mismatch")
    if run["mode"] != "replay" and (
        observation["run_id"] != run["id"] or observation["attempt_epoch"] != run["attempt_epoch"]
    ):
        raise SealError("stale_observation")
    url = public_url(snapshot["url"])
    c.execute(
        """INSERT INTO seal_document(id,source_id,namespace,identity,url)
           VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (uid(), source["id"], run["namespace"], url, url),
    )
    document = one(
        c,
        """SELECT * FROM seal_document WHERE source_id=%s AND namespace=%s
           AND identity=%s FOR UPDATE""",
        (source["id"], run["namespace"], url),
    )
    previous = (
        one(c, "SELECT * FROM seal_revision WHERE id=%s", (document["latest_revision"],))
        if document["latest_revision"]
        else None
    )
    revision_id = previous["id"] if previous else None
    if previous is None or previous["body_hash"] != snapshot["body_hash"]:
        if previous:
            previous_observation = one(
                c, "SELECT * FROM seal_fetch_observation WHERE id=%s", (previous["observation_id"],)
            )
            if (
                previous_observation["run_id"] == run["id"]
                and previous_observation["attempt_epoch"] == run["attempt_epoch"]
            ):
                c.execute(
                    "UPDATE seal_run SET errors=errors || %s WHERE id=%s",
                    (j(["unstable_resource_input"]), run["id"]),
                )
        row = c.execute(
            """INSERT INTO seal_revision
               (id,document_id,predecessor,body_hash,snapshot_id,observation_id)
               VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(document_id,observation_id)
               DO NOTHING RETURNING id""",
            (
                uid(),
                document["id"],
                revision_id,
                snapshot["body_hash"],
                entry["snapshot_id"],
                entry["observation_id"],
            ),
        ).fetchone()
        revision_id = (
            row["id"]
            if row
            else one(
                c,
                "SELECT id FROM seal_revision WHERE document_id=%s AND observation_id=%s",
                (document["id"], entry["observation_id"]),
            )["id"]
        )
    c.execute(
        "UPDATE seal_document SET latest_revision=%s,latest_observation=%s WHERE id=%s",
        (revision_id, entry["observation_id"], document["id"]),
    )
    resource_input = {
        "document_id": document["id"],
        "revision_id": revision_id,
        "snapshot_id": entry["snapshot_id"],
        "observation_id": entry["observation_id"],
    }
    if known is not None:
        known["resource_input"] = resource_input
        c.execute("UPDATE seal_run SET inputs=%s WHERE id=%s", (j(run["inputs"]), run["id"]))
    return resource_input


def stage_record_resource(context, entry):
    """Archive URL revisions for record.v1 responses, including empty responses.

    InputMiddleware calls this after saving its input. Historical generic-document
    bindings continue to use their unchanged Candidate path.
    """
    if context["config"].get("output_schema") != "record.v1":
        return None
    with connect() as c:
        source, run = locked_run(c, context["id"])
        fenced(source, run, context["attempt_epoch"])
        return _stage_resource(c, source, run, entry)


def stage_record(context, item, validation=None):
    output, lineage = validate_record(item, validation)
    with connect() as c:
        source, run = locked_run(c, context["id"])
        fenced(source, run, context["attempt_epoch"])
        for entry in lineage:
            if not any(
                entry["snapshot_id"] == known["snapshot_id"]
                and entry["observation_id"] == known["observation_id"]
                for known in run["inputs"]
            ):
                raise SealError("record_lineage_mismatch")
            observation = one(
                c, "SELECT * FROM seal_fetch_observation WHERE id=%s", (entry["observation_id"],)
            )
            if (
                observation["source_id"] != source["id"]
                or observation["snapshot_id"] != entry["snapshot_id"]
            ):
                raise SealError("record_observation_mismatch")
            if run["mode"] != "replay" and (
                observation["run_id"] != run["id"]
                or observation["attempt_epoch"] != run["attempt_epoch"]
            ):
                raise SealError("stale_observation")
        parent = output["frozen_parent_request"]
        if parent and not any(
            known["snapshot_id"] in {entry["snapshot_id"] for entry in lineage}
            and public_url(known["logical_url"]) == parent["url"]
            and known["role"] == parent["role"]
            and known["method"] == parent["method"]
            for known in run["inputs"]
        ):
            raise SealError("record_parent_request_mismatch")
        for entry in lineage:
            _stage_resource(c, source, run, entry)
        c.execute(
            """INSERT INTO seal_record(id,source_id,namespace,record_type,record_key)
               VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (uid(), source["id"], run["namespace"], output["record_type"], output["record_key"]),
        )
        record = one(
            c,
            """SELECT * FROM seal_record WHERE source_id=%s AND namespace=%s
               AND record_type=%s AND record_key=%s FOR UPDATE""",
            (source["id"], run["namespace"], output["record_type"], output["record_key"]),
        )
        input_ids = sorted({entry["snapshot_id"] for entry in lineage})
        processing_key = digest(
            {"record": record["id"], "binding": run["binding_id"], "inputs": input_ids}
        )
        output_hash = digest(output)
        content_hash = digest(
            {key: output[key] for key in ("record_type", "schema_version", "data")}
        )
        result = c.execute(
            "SELECT id FROM seal_record_result WHERE processing_key=%s AND output_hash=%s",
            (processing_key, output_hash),
        ).fetchone()
        result_id = result["id"] if result else uid()
        if result is None:
            c.execute(
                """INSERT INTO seal_record_result
                   (id,record_id,binding_id,processing_key,output_hash,content_hash,inputs,candidate,checks)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    result_id,
                    record["id"],
                    run["binding_id"],
                    processing_key,
                    output_hash,
                    content_hash,
                    j(input_ids),
                    j(output),
                    j(
                        {
                            "input_verified": True,
                            "locators_verified": True,
                            "identity_verified": True,
                        }
                    ),
                ),
            )
        c.execute(
            """INSERT INTO seal_record_emission
               (id,run_id,attempt_epoch,record_id,result_id,observation_id,inputs)
               VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                uid(),
                run["id"],
                run["attempt_epoch"],
                record["id"],
                result_id,
                item["observation_id"],
                j(lineage),
            ),
        )


def _baseline(c, run, record_id, binding_only):
    return c.execute(
        """SELECT result.*, output->>'record_version_id' AS version_id,
             output->'inputs' AS emission_inputs
           FROM seal_run prior
           CROSS JOIN LATERAL jsonb_array_elements(
             COALESCE(prior.report->'record_outputs','[]')) output
           JOIN seal_record_result result ON result.id=output->>'record_result_id'
           WHERE prior.source_id=%s AND prior.status='complete'
             AND prior.namespace=%s AND result.record_id=%s
             AND (NOT %s OR result.binding_id=%s)
           ORDER BY prior.run_seq DESC, prior.created_at DESC LIMIT 1""",
        (run["source_id"], run["namespace"], record_id, binding_only, run["binding_id"]),
    ).fetchone()


def _raw_state(input_ids):
    state = defaultdict(set)
    for key in input_ids:
        snapshot = Objects().json(key)
        state[public_url(snapshot["url"])].add(snapshot["body_hash"])
    return state


def finish_records(c, source, run):
    """Finalize valid attempt emissions; call accept_records only for a complete Run.

    Conflicting identities keep every immutable candidate but produce no accepted
    output. Missing records never imply deletion, even for a complete empty run.
    """
    fenced(source, run, run["attempt_epoch"])
    emissions = c.execute(
        """SELECT emission.id AS emission_id, emission.inputs AS emission_inputs,
             emission.observation_id, result.*
           FROM seal_record_emission emission
           JOIN seal_record_result result ON result.id=emission.result_id
           WHERE emission.run_id=%s AND emission.attempt_epoch=%s
           ORDER BY emission.record_id, emission.created_at, emission.id""",
        (run["id"], run["attempt_epoch"]),
    ).fetchall()
    grouped = defaultdict(list)
    for emission in emissions:
        grouped[emission["record_id"]].append(emission)
    outputs, errors = [], []
    conflicts, duplicates = 0, 0
    conflicted_record_ids = []
    for record_id, rows in grouped.items():
        if len({row["content_hash"] for row in rows}) != 1:
            conflicts += 1
            conflicted_record_ids.append(record_id)
            errors.append("record_identity_conflict")
            continue
        processing_keys = list({row["processing_key"] for row in rows})
        historical_hashes = c.execute(
            """SELECT processing_key FROM seal_record_result
               WHERE processing_key=ANY(%s) GROUP BY processing_key
               HAVING count(DISTINCT content_hash)>1""",
            (processing_keys,),
        ).fetchall()
        if historical_hashes:
            conflicts += 1
            conflicted_record_ids.append(record_id)
            errors.append("nondeterministic_record_output")
            continue
        duplicates += len(rows) - 1
        record = one(c, "SELECT * FROM seal_record WHERE id=%s FOR UPDATE", (record_id,))
        candidate = rows[0]
        previous = _baseline(c, run, record_id, True)
        global_previous = previous or _baseline(c, run, record_id, False)
        changed = previous is None or previous["content_hash"] != candidate["content_hash"]
        reason = (
            "unchanged"
            if not changed
            else "source_updated"
            if previous
            else "reprocessed"
            if global_previous
            else "first_seen"
        )
        version_id = previous["version_id"] if not changed else None
        if (
            changed
            and global_previous
            and global_previous["content_hash"] == candidate["content_hash"]
        ):
            version_id, reason = global_previous["version_id"], "unchanged"
        if version_id is None:
            version = c.execute(
                """SELECT id FROM seal_record_version WHERE record_id=%s AND binding_id=%s
                   AND run_id=%s AND attempt_epoch=%s""",
                (record_id, run["binding_id"], run["id"], run["attempt_epoch"]),
            ).fetchone()
            version_id = version["id"] if version else uid()
            if version is None:
                c.execute(
                    """INSERT INTO seal_record_version
                       (id,record_id,binding_id,predecessor,run_id,attempt_epoch,
                        content_hash,schema_version,data,change_reason)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        version_id,
                        record_id,
                        run["binding_id"],
                        previous["version_id"] if previous else None,
                        run["id"],
                        run["attempt_epoch"],
                        candidate["content_hash"],
                        candidate["candidate"]["schema_version"],
                        j(candidate["candidate"]["data"]),
                        reason,
                    ),
                )
        lineage = []
        for row in rows:
            for entry in row["emission_inputs"]:
                if entry not in lineage:
                    lineage.append(entry)
        raw_changed = False
        input_set_changed = False
        try:
            if previous:
                before = _raw_state({entry["snapshot_id"] for entry in previous["emission_inputs"]})
                after = _raw_state({entry["snapshot_id"] for entry in lineage})
                raw_changed = any(before[url] != after[url] for url in before.keys() & after.keys())
                input_set_changed = before.keys() != after.keys()
        except SealError as exc:
            errors.append(exc.code)
            raw_changed = None
            input_set_changed = None
        outputs.append(
            {
                "record_id": record["id"],
                "record_version_id": version_id,
                "record_result_id": candidate["id"],
                "record_result_ids": sorted({row["id"] for row in rows}),
                "observation_id": candidate["observation_id"],
                "inputs": lineage,
                "resource_inputs": [
                    entry["resource_input"]
                    for entry in run["inputs"]
                    if entry.get("resource_input")
                    and any(
                        entry["snapshot_id"] == used["snapshot_id"]
                        and entry["observation_id"] == used["observation_id"]
                        for used in lineage
                    )
                ],
                "content_change": reason,
                "raw_changed": raw_changed,
                "input_set_changed": input_set_changed,
                "reprocessed": global_previous is not None and previous is None,
            }
        )
    return {
        "outputs": outputs,
        "errors": sorted(set(errors)),
        "counts": {
            "emission_count": len(emissions),
            "record_count": len(outputs),
            "duplicate_count": duplicates,
            "conflict_count": conflicts,
            "conflicted_record_ids": sorted(conflicted_record_ids),
        },
    }


def accept_records(c, source, run, outputs):
    """Advance current pointers in the completion transaction, after every check."""
    fenced(source, run, run["attempt_epoch"])
    for output in outputs:
        result = one(
            c, "SELECT * FROM seal_record_result WHERE id=%s", (output["record_result_id"],)
        )
        candidate = result["candidate"]
        c.execute(
            """UPDATE seal_record SET latest_version=%s,latest_result=%s,detail_url=%s,
               parent_request=%s
               WHERE id=%s AND source_id=%s AND namespace=%s""",
            (
                output["record_version_id"],
                result["id"],
                candidate["detail_url"],
                j(candidate["frozen_parent_request"]),
                output["record_id"],
                source["id"],
                run["namespace"],
            ),
        )
        if run["mode"] != "replay":
            c.execute(
                "UPDATE seal_fetch_observation SET eligible=true WHERE id=ANY(%s)",
                ([entry["observation_id"] for entry in output["inputs"]],),
            )
