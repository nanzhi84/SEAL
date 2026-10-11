"""Business evidence from saved, reproducible document versions."""

from .core import Objects, SealError, canonical, public_url
from .db import connect, one


def located_document_value(snapshot, body, locator):
    from .document_parser import parse_document, parser_version
    from .record_validation import json_pointer, typed_equal

    if set(locator) - {"kind", "version_id", "pointer", "snapshot_id"}:
        raise SealError("invalid_document_locator")
    with connect() as c:
        version = one(
            c, "SELECT * FROM seal_document_version WHERE id=%s", (locator["version_id"],)
        )
    if version["body_hash"] != snapshot["body_hash"]:
        raise SealError("document_locator_input_mismatch")
    original = Objects().json(version["snapshot_id"])
    if public_url(original["url"]) != public_url(snapshot["url"]):
        raise SealError("document_locator_input_mismatch")
    if version["parser_version"] != parser_version():
        raise SealError("document_parser_version_mismatch")
    content = parse_document(snapshot, body, options=version["parse_options"])
    stored = dict(
        version["content"], children=[dict(node) for node in version["content"]["children"]]
    )
    for node in stored["children"]:
        if node["type"] == "document_ref":
            node.pop("document_id", None)
    if not typed_equal(stored, content):
        raise SealError("document_recompute_mismatch")
    # Only parsed content can be field evidence; generated database IDs cannot.
    return json_pointer(canonical(content), locator.get("pointer"))


def validate_document_inputs(c, source, run, output, lineage, required=False):
    refs = output.get("document_versions", [])
    expected = {(entry["snapshot_id"], entry["observation_id"]) for entry in lineage}
    represented, versions = set(), {}
    for ref in refs:
        if set(ref) != {"document_id", "document_version_id", "snapshot_id", "observation_id"}:
            raise SealError("invalid_record_document_reference")
        if (ref["snapshot_id"], ref["observation_id"]) not in expected:
            raise SealError("record_document_input_mismatch")
        row = c.execute(
            """SELECT v.*,d.source_id,d.namespace,d.url FROM seal_document_version v
               JOIN seal_document d ON d.id=v.document_id WHERE v.id=%s""",
            (ref["document_version_id"],),
        ).fetchone()
        if row is None or (
            row["document_id"] != ref["document_id"]
            or row["source_id"] != source["id"]
            or row["namespace"] != run["namespace"]
        ):
            raise SealError("record_document_reference_mismatch")
        snapshot = Objects().json(ref["snapshot_id"])
        if row["body_hash"] != snapshot["body_hash"] or public_url(row["url"]) != public_url(
            snapshot["url"]
        ):
            raise SealError("record_document_input_mismatch")
        emission = c.execute(
            """SELECT 1 FROM seal_document_emission WHERE document_version_id=%s AND run_id=%s
               AND attempt_epoch=%s AND snapshot_id=%s AND observation_id=%s""",
            (
                ref["document_version_id"],
                run["id"],
                run["attempt_epoch"],
                ref["snapshot_id"],
                ref["observation_id"],
            ),
        ).fetchone()
        if emission is None:
            raise SealError("record_document_not_saved")
        represented.add((ref["snapshot_id"], ref["observation_id"]))
        versions.setdefault(ref["document_version_id"], set()).add(ref["snapshot_id"])
    if required and represented != expected:
        raise SealError("record_document_required")
    main = output["primary_snapshot_id"]
    locators = [(locator, main) for locator in output["locators"].values()]
    locators.append((output["key_locator"], main))
    for evidence in output.get("field_evidence", {}).values():
        locators.extend(
            (proof, evidence["snapshot_id"]) for proof in evidence.get("supporting_locators", [])
        )
    while locators:
        locator, default_input = locators.pop()
        actual_input = locator.get("snapshot_id", default_input)
        if locator.get("kind") == "values":
            locators.extend((part, actual_input) for part in locator["locators"])
        elif locator.get("kind") == "document" and actual_input not in versions.get(
            locator.get("version_id"), set()
        ):
            raise SealError("record_document_locator_unlinked")
