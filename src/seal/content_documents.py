"""Validated, immutable document projections linked to archived Runtime inputs."""

from urllib.parse import urlsplit

from .content_parser import PARSER_VERSION, parse_document
from .core import Objects, SealError, digest, public_url, uid
from .db import connect, fenced, j, locked_run, one
from .scope import paths_for


def _document_id(source_id, namespace, url, method="GET", request_body_sha256=""):
    return digest(
        {
            "source": source_id,
            "namespace": namespace,
            "url": url,
            "method": method,
            "request_body_sha256": request_body_sha256,
        }
    )


def _validate(tree, config, url):
    if set(tree) != {"type", "children"} or tree["type"] != "document":
        raise SealError("invalid_content_document")
    children = tree["children"]
    if not isinstance(children, list) or not 0 < len(children) <= 10000:
        raise SealError("invalid_content_children")
    for child in children:
        if not isinstance(child, dict):
            raise SealError("invalid_content_node")
        kind = child.get("type")
        if kind == "article":
            if set(child) != {"type", "text"} or not isinstance(child["text"], str):
                raise SealError("invalid_content_article")
        elif kind == "table":
            if set(child) != {"type", "name", "columns", "rows"}:
                raise SealError("invalid_content_table")
            if child["name"] is not None and not isinstance(child["name"], str):
                raise SealError("invalid_content_table")
            rows = child["rows"]
            if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
                raise SealError("invalid_content_table")
            width = len(rows[0]) if rows else 0
            if any(len(row) != width for row in rows):
                raise SealError("invalid_content_table")
            if child["columns"] is not None and (
                not isinstance(child["columns"], list) or len(child["columns"]) != width
            ):
                raise SealError("invalid_content_table")
        elif kind == "json":
            if set(child) != {"type", "value"}:
                raise SealError("invalid_content_json")
        elif kind == "image":
            if set(child) != {"type", "url", "caption"} and set(child) != {
                "type",
                "url",
                "caption",
                "snapshot_id",
                "body_hash",
            }:
                raise SealError("invalid_content_image")
            public_url(child["url"])
            if child["caption"] is not None and not isinstance(child["caption"], str):
                raise SealError("invalid_content_image")
        elif kind == "document_ref":
            if set(child) != {"type", "url", "name"}:
                raise SealError("invalid_content_ref")
            target = public_url(child["url"])
            parsed = urlsplit(target)
            if not paths_for(config, parsed.hostname, parsed.path):
                raise SealError("content_ref_out_of_scope")
        else:
            raise SealError("invalid_content_node")
    public_url(url)


def stage_content(context, response, entry):
    snapshot = Objects().json(entry["snapshot_id"])
    if snapshot["status"] != 200 or snapshot["method"] not in {"GET", "POST"}:
        return
    tree, kind, mime = parse_document(
        response, context["config"], entry["snapshot_id"], snapshot["body_hash"]
    )
    url = public_url(snapshot["url"])
    if public_url(response.url) != url or digest(response.body) != snapshot["body_hash"]:
        raise SealError("content_input_mismatch")
    _validate(tree, context["config"], url)
    with connect() as c:
        source, run = locked_run(c, context["id"])
        fenced(source, run, context["attempt_epoch"])
        if not any(
            i["snapshot_id"] == entry["snapshot_id"]
            and i["observation_id"] == entry["observation_id"]
            for i in run["inputs"]
        ):
            raise SealError("content_input_not_in_run")
        observation = one(
            c, "SELECT * FROM seal_fetch_observation WHERE id=%s", (entry["observation_id"],)
        )
        if (
            observation["source_id"] != source["id"]
            or observation["snapshot_id"] != entry["snapshot_id"]
        ):
            raise SealError("content_observation_mismatch")
        if run["mode"] != "replay" and (
            observation["run_id"] != run["id"]
            or observation["attempt_epoch"] != run["attempt_epoch"]
        ):
            raise SealError("stale_observation")
        method = snapshot["method"]
        request_body_sha256 = entry.get("request_body_sha256", "")
        if method == "POST" and not request_body_sha256:
            raise SealError("content_post_body_missing")
        identity = _document_id(source["id"], run["namespace"], url, method, request_body_sha256)
        identities = {identity: (url, method, request_body_sha256)}
        refs = []
        for ordinal, child in enumerate(tree["children"]):
            if child["type"] != "document_ref":
                continue
            target_url = public_url(child["url"])
            target_id = _document_id(source["id"], run["namespace"], target_url)
            identities[target_id] = (target_url, "GET", "")
            child["document_id"] = target_id
            refs.append((ordinal, target_id, target_url))
        # All potentially shared identities use one lock order across callbacks.
        for document_id in sorted(identities):
            document_url, document_method, body_sha = identities[document_id]
            c.execute(
                """INSERT INTO seal_content_document
                   (id,source_id,namespace,url,method,request_body_sha256)
                   VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    document_id,
                    source["id"],
                    run["namespace"],
                    document_url,
                    document_method,
                    body_sha,
                ),
            )
        one(c, "SELECT id FROM seal_content_document WHERE id=%s FOR UPDATE", (identity,))
        processing_key = digest(
            {
                "document": identity,
                "binding": run["binding_id"],
                "snapshot": entry["snapshot_id"],
                "parser": PARSER_VERSION,
            }
        )
        row = c.execute(
            "SELECT id,content_hash FROM seal_content_version WHERE processing_key=%s",
            (processing_key,),
        ).fetchone()
        content_hash = digest(tree)
        if row and row["content_hash"] != content_hash:
            raise SealError("nondeterministic_content_output")
        version_id = row["id"] if row else uid()
        if row is None:
            c.execute(
                """INSERT INTO seal_content_version
                   (id,document_id,binding_id,run_id,attempt_epoch,snapshot_id,observation_id,
                    body_hash,parser_version,processing_key,content_hash,tree)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    version_id,
                    identity,
                    run["binding_id"],
                    run["id"],
                    run["attempt_epoch"],
                    entry["snapshot_id"],
                    entry["observation_id"],
                    snapshot["body_hash"],
                    PARSER_VERSION,
                    processing_key,
                    content_hash,
                    j(tree),
                ),
            )
            for ordinal, target_id, target_url in refs:
                c.execute(
                    """INSERT INTO seal_content_ref(version_id,ordinal,target_document_id,url)
                       VALUES(%s,%s,%s,%s)""",
                    (version_id, ordinal, target_id, target_url),
                )
        c.execute(
            """INSERT INTO seal_content_emission
               (id,run_id,attempt_epoch,version_id,observation_id,snapshot_id)
               VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                uid(),
                run["id"],
                run["attempt_epoch"],
                version_id,
                entry["observation_id"],
                entry["snapshot_id"],
            ),
        )
    return {
        "document_id": identity,
        "document_version_id": version_id,
        "format": kind,
        "mime": mime,
    }


def input_versions(connection, run, lineage):
    versions = []
    for entry in lineage:
        row = connection.execute(
            """SELECT v.id FROM seal_content_emission e JOIN seal_content_version v ON v.id=e.version_id
               WHERE e.run_id=%s AND e.attempt_epoch=%s AND e.snapshot_id=%s AND e.observation_id=%s
               ORDER BY e.created_at DESC LIMIT 1""",
            (run["id"], run["attempt_epoch"], entry["snapshot_id"], entry["observation_id"]),
        ).fetchone()
        if row is None:
            raise SealError("business_document_missing")
        versions.append(row["id"])
    return sorted(set(versions))


def run_outputs(connection, run):
    return connection.execute(
        """SELECT DISTINCT ON (v.document_id) v.document_id,v.id AS version_id,
                  e.snapshot_id,e.observation_id
           FROM seal_content_emission e JOIN seal_content_version v ON v.id=e.version_id
           WHERE e.run_id=%s AND e.attempt_epoch=%s
           ORDER BY v.document_id,e.created_at DESC,e.id DESC""",
        (run["id"], run["attempt_epoch"]),
    ).fetchall()


def accept_outputs(connection, outputs):
    for row in outputs:
        connection.execute(
            "UPDATE seal_content_document SET latest_version=%s WHERE id=%s",
            (row["version_id"], row["document_id"]),
        )


def export_content(connection, source, run_id=None):
    if run_id:
        rows = connection.execute(
            """SELECT DISTINCT ON (d.id) d.id AS document_id,d.url,d.method,
               d.request_body_sha256,d.latest_version,
               v.*,e.observation_id AS actual_observation_id
               FROM seal_content_emission e JOIN seal_content_version v ON v.id=e.version_id
               JOIN seal_run run ON run.id=e.run_id AND run.attempt_epoch=e.attempt_epoch
               JOIN seal_content_document d ON d.id=v.document_id
               WHERE d.source_id=%s AND e.run_id=%s
               ORDER BY d.id,e.created_at DESC,e.id DESC""",
            (source["id"], run_id),
        ).fetchall()
    else:
        rows = connection.execute(
            """SELECT d.id AS document_id,d.url,d.method,d.request_body_sha256,d.latest_version,v.*,
                      v.observation_id AS actual_observation_id
               FROM seal_content_document d JOIN seal_content_version v ON v.id=d.latest_version
               WHERE d.source_id=%s ORDER BY d.url""",
            (source["id"],),
        ).fetchall()
    results, unavailable = [], []
    for row in rows:
        try:
            snapshot = Objects().json(row["snapshot_id"])
            if snapshot["body_hash"] != row["body_hash"]:
                raise SealError("content_snapshot_mismatch")
            body = Objects().get(row["body_hash"])
            if len(body) != snapshot["body_size"]:
                raise SealError("content_body_size_mismatch")
        except SealError as exc:
            unavailable.append(
                {
                    "document_id": row["document_id"],
                    "document_version_id": row["id"],
                    "reason": exc.code,
                }
            )
            continue
        refs = connection.execute(
            """SELECT r.ordinal,r.target_document_id AS document_id,r.url,
               (target.latest_version IS NOT NULL OR EXISTS
                 (SELECT 1 FROM seal_content_emission e JOIN seal_content_version v
                  ON v.id=e.version_id JOIN seal_run run
                  ON run.id=e.run_id AND run.attempt_epoch=e.attempt_epoch
                  WHERE v.document_id=target.id AND e.run_id=%s)) AS resolved
               FROM seal_content_ref r JOIN seal_content_document target ON target.id=r.target_document_id
               WHERE r.version_id=%s ORDER BY r.ordinal""",
            (run_id, row["id"]),
        ).fetchall()
        results.append(
            {
                "document_id": row["document_id"],
                "document_version_id": row["id"],
                "url": row["url"],
                "method": row["method"],
                "request_body_sha256": row["request_body_sha256"],
                "type": "document",
                "children": row["tree"]["children"],
                "refs": refs,
                "snapshot_id": row["snapshot_id"],
                "observation_id": row["actual_observation_id"],
                "body_hash": row["body_hash"],
                "parser_version": row["parser_version"],
                "binding_id": row["binding_id"],
                "content_hash": row["content_hash"],
                "stale": row["latest_version"] != row["id"],
            }
        )
    return results, unavailable
