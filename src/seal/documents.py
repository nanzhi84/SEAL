"""Verified unified document trees and immutable parsing/input associations."""

from copy import deepcopy

from .core import Objects, SealError, canonical, digest, public_url, uid
from .db import connect, fenced, j, locked_run, one

CONTENT_TYPES = {"article", "image", "table", "json", "document_ref"}


def validate_content(content, *, resolved=False):
    """Validate the small content contract without asserting semantic accuracy."""
    from .record_validation import json_value

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
    """Build an item from archived bytes, suitable for both Collect and Replay."""
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


def document_item(response, options=None):
    return parse_snapshot(
        response.meta["seal_snapshot_id"], response.meta["seal_observation_id"], options
    )


def _identity(c, source, run, url):
    url = public_url(url)
    c.execute(
        """INSERT INTO seal_document(id,source_id,namespace,identity,url)
           VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (uid(), source["id"], run["namespace"], url, url),
    )
    return one(
        c,
        """SELECT * FROM seal_document WHERE source_id=%s AND namespace=%s
           AND identity=%s FOR UPDATE""",
        (source["id"], run["namespace"], url),
    )


def stage_document(context, item):
    """Save only reproducible trees; bind URL references to real resource identities."""
    from .document_parser import parser_version
    from .records import _stage_resource

    if (
        not isinstance(item, dict)
        or set(item)
        != {"type", "snapshot_id", "observation_id", "parser_version", "options", "content"}
        or item["type"] != "parsed_document"
        or not isinstance(item["options"], dict)
    ):
        raise SealError("invalid_document_item")
    validate_content(item["content"])
    entry = {key: item[key] for key in ("snapshot_id", "observation_id")}
    # Reject foreign/missing evidence before accessing attacker-supplied archive keys.
    # Recheck under Source -> Run locks after the potentially expensive pure parse.
    with connect() as c:
        inputs = one(c, "SELECT inputs FROM seal_run WHERE id=%s", (context["id"],))["inputs"]
        if not any(
            all(known.get(key) == value for key, value in entry.items()) for known in inputs
        ):
            raise SealError("document_lineage_mismatch")
    if item["parser_version"] != parser_version():
        raise SealError("document_parser_version_mismatch")
    verified = parse_snapshot(item["snapshot_id"], item["observation_id"], item["options"])
    if canonical(verified["content"]) != canonical(item["content"]):
        raise SealError("document_content_mismatch")
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
            c, "SELECT * FROM seal_fetch_observation WHERE id=%s", (item["observation_id"],)
        )
        if (
            observation["source_id"] != source["id"]
            or observation["snapshot_id"] != item["snapshot_id"]
            or observation["body_hash"] != snapshot["body_hash"]
        ):
            raise SealError("document_observation_mismatch")
        if run["mode"] != "replay" and (
            observation["run_id"] != run["id"]
            or observation["attempt_epoch"] != run["attempt_epoch"]
        ):
            raise SealError("stale_observation")
        resource = _stage_resource(c, source, run, entry)
        if resource is None:
            raise SealError("invalid_document_response")
        document = one(c, "SELECT * FROM seal_document WHERE id=%s", (resource["document_id"],))
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
            "SELECT * FROM seal_document_version WHERE processing_key=%s", (key,)
        ).fetchone()
        if previous and previous["content_hash"] != content_hash:
            raise SealError("nondeterministic_document_output")
        version_id = previous["id"] if previous else uid()
        if previous is None:
            binding = one(
                c, "SELECT recipe_version FROM seal_binding WHERE id=%s", (run["binding_id"],)
            )
            c.execute(
                """INSERT INTO seal_document_version
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
                    """INSERT INTO seal_document_reference
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
            """INSERT INTO seal_document_emission
               (id,run_id,attempt_epoch,document_id,document_version_id,snapshot_id,observation_id)
               VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                uid(),
                run["id"],
                run["attempt_epoch"],
                document["id"],
                version_id,
                item["snapshot_id"],
                item["observation_id"],
            ),
        )
        return dict(entry, document_id=document["id"], document_version_id=version_id)


def finish_documents(c, source, run):
    """Freeze successful parse evidence independently of optional business extraction."""
    fenced(source, run, run["attempt_epoch"])
    rows = c.execute(
        """SELECT e.*,v.content_hash,v.body_hash FROM seal_document_emission e
           JOIN seal_document_version v ON v.id=e.document_version_id
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
            """SELECT r.body_hash FROM seal_document d JOIN seal_revision r
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
    if run["mode"] == "replay":
        return
    for output in outputs:
        c.execute(
            """UPDATE seal_document SET latest_document_version=%s WHERE id=%s
               AND source_id=%s AND namespace=%s""",
            (output["document_version_id"], output["document_id"], source["id"], run["namespace"]),
        )
        c.execute(
            "UPDATE seal_fetch_observation SET eligible=true WHERE id=ANY(%s)",
            ([entry["observation_id"] for entry in output["inputs"]],),
        )


def resolve_references(c, version_id):
    """Report query-time target state without mutating frozen version content."""
    rows = c.execute(
        """SELECT r.*,d.latest_document_version,d.source_id,d.namespace
           FROM seal_document_reference r JOIN seal_document d ON d.id=r.target_document_id
           WHERE r.document_version_id=%s ORDER BY r.ordinal""",
        (version_id,),
    ).fetchall()
    values = []
    for row in rows:
        # A valid staged tree remains inspectable after a failed Run or isolated Replay.
        latest = row["latest_document_version"] or (
            c.execute(
                "SELECT id FROM seal_document_version WHERE document_id=%s ORDER BY created_at DESC,id LIMIT 1",
                (row["target_document_id"],),
            ).fetchone()
            or {}
        ).get("id")
        failed = c.execute(
            """SELECT discovery.reason FROM seal_run run JOIN seal_binding b ON b.id=run.binding_id
               CROSS JOIN LATERAL jsonb_array_elements(run.inputs) input
               JOIN seal_fetch_observation o ON o.id=input->>'observation_id'
                 AND o.snapshot_id=input->>'snapshot_id'
               JOIN seal_discovery discovery ON discovery.run_id=run.id
                 AND discovery.attempt_epoch=run.attempt_epoch
                 AND discovery.snapshot_id=o.snapshot_id
               WHERE run.source_id=%s AND run.namespace=%s AND (o.url=%s OR o.final_url=%s)
                 AND b.config->>'output_schema'='document.v1' AND o.status=200
                 AND discovery.state='failed' AND (
                   discovery.reason ~ '^(document_|invalid_document_|html_|pdf_|docx_|xlsx?_|csv_|json_|image_)'
                   OR discovery.reason IN ('empty_document','unsupported_content_type',
                     'access_control_detected','ambiguous_or_missing_field','fallback_error_page',
                     'fallback_non_document','nondeterministic_document_output'))
               ORDER BY run.created_at DESC LIMIT 1""",
            (row["source_id"], row["namespace"], row["url"], row["url"]),
        ).fetchone()
        values.append(
            {
                "ordinal": row["ordinal"],
                "document_id": row["target_document_id"],
                "url": row["url"],
                "name": row["name"],
                "relation": row["relation"],
                "target_version_id": latest,
                "state": "parsed" if latest else "parse_failed" if failed else "not_fetched",
                "state_scope": "query_time",
                "reason": failed["reason"] if failed and not latest else None,
            }
        )
    return values


def version_view(c, version, *, inputs=None, run=None):
    document = one(c, "SELECT * FROM seal_document WHERE id=%s", (version["document_id"],))
    snapshot = Objects().json(version["snapshot_id"])
    body = Objects().get(snapshot["body_hash"])
    if len(body) != snapshot["body_size"]:
        raise SealError("snapshot_body_size_mismatch")
    frozen_inputs = inputs or [
        {"snapshot_id": version["snapshot_id"], "observation_id": version["observation_id"]}
    ]
    for entry in frozen_inputs:
        evidence = Objects().json(entry["snapshot_id"])
        wire = Objects().get(evidence["body_hash"])
        if len(wire) != evidence["body_size"] or evidence["body_hash"] != version["body_hash"]:
            raise SealError("document_input_mismatch")
        observed = one(
            c,
            "SELECT source_id,snapshot_id FROM seal_fetch_observation WHERE id=%s",
            (entry["observation_id"],),
        )
        if (
            observed["source_id"] != document["source_id"]
            or observed["snapshot_id"] != entry["snapshot_id"]
        ):
            raise SealError("document_observation_mismatch")
    return {
        "id": document["id"],
        "type": "document",
        "url": document["url"],
        "source_id": document["source_id"],
        "namespace": document["namespace"],
        "document_id": document["id"],
        "document_version_id": version["id"],
        "content": version["content"],
        "children": version["content"]["children"],
        "source_format": version["source_format"],
        "content_hash": version["content_hash"],
        "binding_id": version["binding_id"],
        "recipe_version": version["recipe_version"],
        "parser_version": version["parser_version"],
        "parse_options": version["parse_options"],
        "snapshot_id": version["snapshot_id"],
        "observation_id": version["observation_id"],
        "revision_id": version["revision_id"],
        "body_hash": snapshot["body_hash"],
        "created_at": version["created_at"],
        "inputs": frozen_inputs,
        "references": resolve_references(c, version["id"]),
        "checks": {
            "structure_valid": True,
            "input_verified": True,
            "parser_recomputed": True,
            "quality_status": "not_evaluated",
        },
        **({"run_id": run["id"], "run_status": run["status"], "mode": run["mode"]} if run else {}),
    }


def inspect_document(identity):
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        document = one(c, "SELECT * FROM seal_document WHERE id=%s", (identity,))
        versions, unavailable = [], []
        for version in c.execute(
            "SELECT * FROM seal_document_version WHERE document_id=%s ORDER BY created_at,id",
            (identity,),
        ).fetchall():
            try:
                versions.append(version_view(c, version))
            except SealError as exc:
                unavailable.append({"document_version_id": version["id"], "reason": exc.code})
        return {
            "document": document,
            "versions": versions,
            "unavailable": unavailable,
            "emissions": c.execute(
                "SELECT * FROM seal_document_emission WHERE document_id=%s ORDER BY created_at,id",
                (identity,),
            ).fetchall(),
        }


def inspect_document_version(identity):
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        version = one(c, "SELECT * FROM seal_document_version WHERE id=%s", (identity,))
        return version_view(c, version)


def export_document_rows(c, source, run_id=None):
    """Use frozen Run emissions, never today's mutable resource revision pointers."""
    from .manifest import has_table

    if not has_table(c, "seal_document_version"):
        return [], []
    rows = c.execute(
        """WITH selected AS (
          SELECT DISTINCT ON (v.document_id) v.*,run.id AS selected_run_id,
                 run.mode,run.status,output->'inputs' AS emission_inputs
          FROM seal_run run CROSS JOIN LATERAL jsonb_array_elements(
            COALESCE(run.report->'document_outputs','[]')) output
          JOIN seal_document_version v ON v.id=output->>'document_version_id'
          WHERE run.source_id=%s AND (run.id=%s OR (%s::text IS NULL
            AND run.mode IN ('collect','recheck') AND run.status='complete'))
          ORDER BY v.document_id,run.run_seq DESC,run.created_at DESC,run.id
        ) SELECT * FROM selected ORDER BY document_id""",
        (source["id"], run_id, run_id),
    ).fetchall()
    documents, unavailable = [], []
    for row in rows:
        try:
            documents.append(
                version_view(
                    c,
                    row,
                    inputs=row["emission_inputs"],
                    run={
                        "id": row["selected_run_id"],
                        "mode": row["mode"],
                        "status": row["status"],
                    },
                )
            )
        except SealError as exc:
            unavailable.append(
                {
                    "document_id": row["document_id"],
                    "document_version_id": row["id"],
                    "reason": exc.code,
                }
            )
    return documents, unavailable


def export_documents(source_id, run_id=None):
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        source = one(c, "SELECT * FROM seal_source WHERE id=%s", (source_id,))
        if run_id:
            run = one(c, "SELECT * FROM seal_run WHERE id=%s", (run_id,))
            if run["source_id"] != source_id:
                raise SealError("run_source_mismatch")
        documents, unavailable = export_document_rows(c, source, run_id)
        return {"source_id": source_id, "documents": documents, "unavailable": unavailable}
