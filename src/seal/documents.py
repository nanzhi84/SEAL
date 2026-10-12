"""Verified unified document trees and immutable parsing/input associations."""

from copy import deepcopy

from .core import Objects, SealError, canonical, digest, public_url, uid
from .db import connect, fenced, j, locked_run, one

CONTENT_TYPES = {"article", "image", "table", "json", "document_ref"}


def validate_content(content, *, resolved=False):
    """Validate the small content contract without asserting semantic accuracy."""
    from .content import json_value

    if (
        not isinstance(content, dict)
        or set(content) != {"format", "children"}
        or not isinstance(content["format"], str)
        or not content["format"]
        or not isinstance(content["children"], list)
        or len(content["children"]) > 1000
    ):
        raise SealError("invalid_document_structure")
    articles = cells = 0
    for node in content["children"]:
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("type"), str)
            or node["type"] not in CONTENT_TYPES
        ):
            raise SealError("invalid_document_node")
        kind = node["type"]
        if kind == "article":
            articles += 1
            if (
                set(node) != {"type", "text", "format"}
                or node["format"] != "text"
                or not isinstance(node["text"], str)
                or articles > 1
            ):
                raise SealError("invalid_document_article")
        elif kind == "image":
            if (
                not {"type", "url"} <= set(node)
                or set(node) - {"type", "url", "body_hash", "archive_member", "caption"}
                or any(not isinstance(node[key], str) for key in node if key != "type")
                or (
                    "body_hash" in node
                    and (
                        len(node["body_hash"]) != 64
                        or any(c not in "0123456789abcdef" for c in node["body_hash"])
                    )
                )
            ):
                raise SealError("invalid_document_image")
            public_url(node["url"])
        elif kind == "table":
            if (
                not {"type", "name", "rows"} <= set(node)
                or set(node) - {"type", "name", "rows", "columns"}
                or not isinstance(node["name"], str)
                or not isinstance(node["rows"], list)
                or any(not isinstance(row, list) for row in node["rows"])
            ):
                raise SealError("invalid_document_table")
            widths = {len(row) for row in node["rows"]}
            if len(widths) > 1:
                raise SealError("invalid_document_table")
            if "columns" in node and (
                not isinstance(node["columns"], list)
                or any(not isinstance(value, str) for value in node["columns"])
                or (widths and len(node["columns"]) != next(iter(widths)))
            ):
                raise SealError("invalid_document_table")
            for row in node["rows"]:
                cells += len(row)
                if cells > 500000:
                    raise SealError("document_cell_budget_exceeded")
                if any(isinstance(value, (dict, list)) for value in row):
                    raise SealError("invalid_document_table")
                json_value(row)
        elif kind == "json":
            if set(node) != {"type", "value"}:
                raise SealError("invalid_document_json")
            json_value(node["value"])
        elif kind == "document_ref":
            required = {"type", "url", "relation"} | ({"document_id"} if resolved else set())
            allowed = required | {"name"}
            if (
                not required <= set(node)
                or set(node) - allowed
                or not isinstance(node["relation"], str)
                or node["relation"] not in {"attachment", "link"}
                or any(not isinstance(node[key], str) for key in node if key != "type")
            ):
                raise SealError("invalid_document_reference")
            public_url(node["url"])
    if len(canonical(content)) > 10000000:
        raise SealError("document_content_budget_exceeded")
    return content


def parse_snapshot(snapshot_id, observation_id, options=None):
    """Parse immutable archived bytes with the shared format parser."""
    from .document_parser import parse_document, parser_version

    snapshot = Objects().json(snapshot_id)
    body = Objects().get(snapshot["body_hash"])
    if len(body) != snapshot["body_size"]:
        raise SealError("snapshot_body_size_mismatch")
    options = {} if options is None else options
    content = parse_document(snapshot, body, options=options)
    validate_content(content)
    return {
        "type": "parsed_document",
        "snapshot_id": snapshot_id,
        "observation_id": observation_id,
        "parser_version": parser_version(),
        "options": options,
        "content": content,
    }


def _identity(c, source, run, url):
    url = public_url(url)
    c.execute(
        """INSERT INTO seal.document(id,source_id,namespace,identity,url)
           VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (uid(), source["id"], run["namespace"], url, url),
    )
    return one(
        c,
        """SELECT * FROM seal.document WHERE source_id=%s AND namespace=%s
           AND identity=%s FOR UPDATE""",
        (source["id"], run["namespace"], url),
    )


def stage_document(context, entry, options=None):
    """Save only reproducible trees; bind URL references to real resource identities."""
    from .resources import _stage_resource

    # The shared parser is the only producer; adapters cannot provide content trees.
    with connect() as c:
        inputs = one(c, "SELECT inputs FROM seal.run WHERE id=%s", (context["id"],))["inputs"]
        if not any(
            all(known.get(key) == value for key, value in entry.items()) for known in inputs
        ):
            raise SealError("document_lineage_mismatch")
    item = parse_snapshot(entry["snapshot_id"], entry["observation_id"], options)
    snapshot = Objects().json(item["snapshot_id"])
    if snapshot["method"] not in {"GET", "POST"} or snapshot["status"] != 200:
        raise SealError("invalid_document_response")
    with connect() as c:
        source, run = locked_run(c, context["id"])
        fenced(source, run, context["attempt_epoch"])
        if not any(
            all(known.get(key) == value for key, value in entry.items()) for known in run["inputs"]
        ):
            raise SealError("document_lineage_mismatch")
        observation = one(
            c, "SELECT * FROM seal.fetch_observation WHERE id=%s", (item["observation_id"],)
        )
        if (
            observation["source_id"] != source["id"]
            or observation["snapshot_id"] != item["snapshot_id"]
            or observation["body_hash"] != snapshot["body_hash"]
        ):
            raise SealError("document_observation_mismatch")
        if (
            observation["run_id"] != run["id"]
            or observation["attempt_epoch"] != run["attempt_epoch"]
        ):
            raise SealError("stale_observation")
        resource = _stage_resource(c, source, run, entry)
        if resource is None:
            raise SealError("invalid_document_response")
        document = one(c, "SELECT * FROM seal.document WHERE id=%s", (resource["document_id"],))
        content = deepcopy(item["content"])
        references = []
        for ordinal, node in enumerate(content["children"]):
            if node["type"] != "document_ref":
                continue
            target = _identity(c, source, run, node["url"])
            node["url"], node["document_id"] = target["url"], target["id"]
            references.append((ordinal, node))
        validate_content(content, resolved=True)
        key = digest(
            {
                "document": document["id"],
                "body_hash": snapshot["body_hash"],
                "interpretation": {
                    "encoding": snapshot.get("encoding"),
                    "response_type": snapshot.get("response_type"),
                    "content_type": next(
                        (
                            value
                            for name, value in snapshot.get("headers", {}).items()
                            if name.lower() == "content-type"
                        ),
                        None,
                    ),
                },
                "binding": run["binding_id"],
                "parser": item["parser_version"],
                "options": item["options"],
            }
        )
        content_hash = digest(content)
        previous = c.execute(
            "SELECT * FROM seal.document_version WHERE processing_key=%s", (key,)
        ).fetchone()
        if previous and previous["content_hash"] != content_hash:
            raise SealError("nondeterministic_document_output")
        version_id = previous["id"] if previous else uid()
        if previous is None:
            binding = one(
                c, "SELECT recipe_version FROM seal.binding WHERE id=%s", (run["binding_id"],)
            )
            c.execute(
                """INSERT INTO seal.document_version
                   (id,document_id,binding_id,recipe_version,revision_id,snapshot_id,body_hash,
                    observation_id,parser_version,parse_options,source_format,content,
                    content_hash,processing_key)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    version_id,
                    document["id"],
                    run["binding_id"],
                    binding["recipe_version"],
                    resource["revision_id"],
                    item["snapshot_id"],
                    snapshot["body_hash"],
                    item["observation_id"],
                    item["parser_version"],
                    j(item["options"]),
                    content["format"],
                    j(content),
                    content_hash,
                    key,
                ),
            )
            for ordinal, node in references:
                c.execute(
                    """INSERT INTO seal.document_reference
                       (document_version_id,ordinal,target_document_id,url,name,relation)
                       VALUES(%s,%s,%s,%s,%s,%s)""",
                    (
                        version_id,
                        ordinal,
                        node["document_id"],
                        node["url"],
                        node.get("name"),
                        node["relation"],
                    ),
                )
        c.execute(
            """INSERT INTO seal.document_emission
               (id,run_id,attempt_epoch,document_id,document_version_id,snapshot_id,observation_id,change_kind)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                uid(),
                run["id"],
                run["attempt_epoch"],
                document["id"],
                version_id,
                item["snapshot_id"],
                item["observation_id"],
                "first_seen"
                if not document["latest_document_version"]
                else "unchanged"
                if document["latest_document_version"] == version_id
                else "updated"
                if one(
                    c,
                    "SELECT body_hash FROM seal.document_version WHERE id=%s",
                    (document["latest_document_version"],),
                )["body_hash"]
                != snapshot["body_hash"]
                else "reprocessed",
            ),
        )
        return dict(entry, document_id=document["id"], document_version_id=version_id)


def finish_documents(c, source, run):
    """Freeze successful parse evidence for this execution attempt."""
    fenced(source, run, run["attempt_epoch"])
    rows = c.execute(
        """SELECT e.*,v.content_hash,v.body_hash FROM seal.document_emission e
           JOIN seal.document_version v ON v.id=e.document_version_id
           WHERE e.run_id=%s AND e.attempt_epoch=%s
           ORDER BY e.document_id,e.created_at,e.id""",
        (run["id"], run["attempt_epoch"]),
    ).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["document_id"], []).append(row)
    outputs, errors, conflicts = [], [], []
    for identity, emissions in grouped.items():
        if len({row["document_version_id"] for row in emissions}) != 1:
            conflicts.append(identity)
            errors.append("document_identity_conflict")
            continue
        last = emissions[-1]
        current = one(
            c,
            """SELECT r.body_hash FROM seal.document d JOIN seal.revision r
               ON r.id=d.latest_revision WHERE d.id=%s FOR UPDATE OF d""",
            (identity,),
        )
        if current["body_hash"] != last["body_hash"]:
            errors.append("stale_document_input")
            continue
        outputs.append(
            {
                "document_id": identity,
                "document_version_id": last["document_version_id"],
                "snapshot_id": last["snapshot_id"],
                "observation_id": last["observation_id"],
                "inputs": [
                    {"snapshot_id": row["snapshot_id"], "observation_id": row["observation_id"]}
                    for row in emissions
                ],
            }
        )
    return {
        "outputs": outputs,
        "errors": sorted(set(errors)),
        "counts": {
            "document_count": len(outputs),
            "emission_count": len(rows),
            "conflict_count": len(conflicts),
            "conflicted_document_ids": conflicts,
        },
    }


def accept_documents(c, source, run, outputs):
    """Promote online current versions only in the successful completion transaction."""
    fenced(source, run, run["attempt_epoch"])
    for output in outputs:
        c.execute(
            """UPDATE seal.document SET latest_document_version=%s WHERE id=%s
               AND source_id=%s AND namespace=%s""",
            (output["document_version_id"], output["document_id"], source["id"], run["namespace"]),
        )
        c.execute(
            "UPDATE seal.fetch_observation SET eligible=true WHERE id=ANY(%s)",
            ([entry["observation_id"] for entry in output["inputs"]],),
        )
