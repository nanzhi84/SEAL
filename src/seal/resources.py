"""Document-level raw resource identities and revisions, shared by all parsers."""

from .core import Objects, SealError, public_url, uid
from .db import j, one


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
    if snapshot["method"] not in {"GET", "POST"} or snapshot["status"] != 200:
        return None
    observation = one(
        c, "SELECT * FROM seal.fetch_observation WHERE id=%s", (entry["observation_id"],)
    )
    if (
        observation["source_id"] != source["id"]
        or observation["snapshot_id"] != entry["snapshot_id"]
    ):
        raise SealError("resource_observation_mismatch")
    if observation["run_id"] != run["id"] or observation["attempt_epoch"] != run["attempt_epoch"]:
        raise SealError("stale_observation")
    url = public_url(snapshot["url"])
    identity = url if snapshot["method"] == "GET" else url + "#post:" + known["request_identity"]
    c.execute(
        """INSERT INTO seal.document(id,source_id,namespace,identity,url)
           VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (uid(), source["id"], run["namespace"], identity, url),
    )
    document = one(
        c,
        """SELECT * FROM seal.document WHERE source_id=%s AND namespace=%s
           AND identity=%s FOR UPDATE""",
        (source["id"], run["namespace"], identity),
    )
    # Compare all representations already staged in this attempt, including A/B/A.
    # Role and redirect origin do not define a different wire representation.
    for prior in run["inputs"]:
        if prior.get("resource_input", {}).get("document_id") != document["id"]:
            continue
        previous_snapshot = Objects().json(prior["snapshot_id"])
        if previous_snapshot["body_hash"] == snapshot["body_hash"]:
            continue
        previous_representation = previous_snapshot.get("representation_id")
        representation = snapshot.get("representation_id")
        if previous_representation and representation and previous_representation != representation:
            continue
        # Old externally persisted snapshots lack representation identity: retain
        # the conservative same-URL rule rather than guessing negotiation headers.
        c.execute(
            "UPDATE seal.run SET errors=errors || %s WHERE id=%s",
            (j(["unstable_resource_input"]), run["id"]),
        )
        break
    previous = (
        one(c, "SELECT * FROM seal.revision WHERE id=%s", (document["latest_revision"],))
        if document["latest_revision"]
        else None
    )
    revision_id = previous["id"] if previous else None
    if previous is None or previous["body_hash"] != snapshot["body_hash"]:
        row = c.execute(
            """INSERT INTO seal.revision
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
                "SELECT id FROM seal.revision WHERE document_id=%s AND observation_id=%s",
                (document["id"], entry["observation_id"]),
            )["id"]
        )
    c.execute(
        "UPDATE seal.document SET latest_revision=%s,latest_observation=%s WHERE id=%s",
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
        c.execute("UPDATE seal.run SET inputs=%s WHERE id=%s", (j(run["inputs"]), run["id"]))
    return resource_input
