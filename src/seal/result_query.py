"""Read-only source selection and immutable successful-version pagination."""

import time
from urllib.parse import urlencode

from .api_security import ApiError, Attachment, Cursor
from .core import SealError, canonical
from .db import connect
from .objects import Objects
from .public_results import envelope, project


def page_rows(source_url, caller, cursor, limit):
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        matches = c.execute(
            "SELECT id FROM seal.source WHERE config->'entry_urls' ? %s ORDER BY id LIMIT 2",
            (source_url,),
        ).fetchall()
        if not matches:
            raise ApiError(404, "source_not_found")
        if len(matches) != 1:
            raise ApiError(409, "source_ambiguous")
        source_id = matches[0]["id"]
        caller.authorize(source_id)
        if cursor and (cursor.source != source_id or cursor.caller != caller.id):
            raise ApiError(400, "invalid_cursor")
        watermark = (
            cursor.watermark
            if cursor
            else c.execute(
                """SELECT coalesce(max(v.cursor),0) AS watermark FROM seal.version v
                JOIN seal.resource r ON r.id=v.resource_id
                JOIN seal.frontier f ON f.id=r.frontier_id WHERE f.source_id=%s""",
                (source_id,),
            ).fetchone()["watermark"]
        )
        if cursor:
            query = """SELECT r.id AS resource_id,r.url,v.id AS version_id,v.result_ref
                FROM seal.frontier f JOIN seal.resource r ON r.frontier_id=f.id
                JOIN LATERAL (SELECT id,result_ref FROM seal.version
                  WHERE resource_id=r.id AND cursor<=%s ORDER BY sequence DESC LIMIT 1) v ON true
                WHERE f.source_id=%s AND r.id COLLATE "C">%s COLLATE "C"
                ORDER BY r.id COLLATE "C" LIMIT %s"""
            params = (watermark, source_id, cursor.after, limit + 1)
        else:
            query = """SELECT r.id AS resource_id,r.url,v.id AS version_id,v.result_ref
                FROM seal.frontier f JOIN seal.resource r ON r.frontier_id=f.id
                JOIN seal.version v ON v.id=r.current_version_id
                WHERE f.source_id=%s ORDER BY r.id COLLATE "C" LIMIT %s"""
            params = (source_id, limit + 1)
        rows = c.execute(query, params).fetchall()
    return source_id, watermark, rows


def results(settings, tokens, caller, source_url, limit, cursor, deadline):
    source_id, watermark, rows = page_rows(source_url, caller, cursor, limit)
    objects, content, size = Objects(), [], 2
    for row in rows[:limit]:
        if time.monotonic() >= deadline:
            raise SealError("archive_read_timeout")
        row["source_id"] = source_id
        value = envelope(
            objects, row["result_ref"], max_bytes=settings.result_max_bytes, deadline=deadline
        )
        # Do not advertise files that cannot be read and verified. Every response is complete.
        for ref in value["attachment_refs"]:
            objects.get(ref, max_bytes=settings.attachment_max_bytes, deadline=deadline)
        item = project(row, value, settings, tokens)
        size += len(canonical(item)) + 1
        if size > settings.page_max_bytes:
            raise SealError("archive_too_large")
        content.append(item)
    link = None
    if len(rows) > limit:
        next_cursor = tokens.encode(
            Cursor(
                caller=caller.id,
                source=source_id,
                watermark=watermark,
                after=rows[limit - 1]["resource_id"],
                issued_at=cursor.issued_at if cursor else int(time.time()),
            )
        )
        link = (
            settings.public_base_url
            + "/results?"
            + urlencode({"source_url": source_url, "limit": limit, "cursor": next_cursor})
        )
    return canonical(content), link


def attachment(settings, tokens, caller, value, deadline):
    identity = tokens.decode(value, Attachment)
    caller.authorize(identity.source)
    with connect() as c:
        c.execute("SET TRANSACTION READ ONLY")
        row = c.execute(
            """SELECT v.result_ref FROM seal.version v
            JOIN seal.resource r ON r.id=v.resource_id JOIN seal.frontier f ON f.id=r.frontier_id
            WHERE v.id=%s AND f.source_id=%s""",
            (identity.version, identity.source),
        ).fetchone()
    if row is None:
        raise ApiError(404, "attachment_not_found")
    objects = Objects()
    value = envelope(
        objects, row["result_ref"], max_bytes=settings.result_max_bytes, deadline=deadline
    )
    if identity.index >= len(value["attachment_refs"]):
        raise ApiError(404, "attachment_not_found")
    return objects.get(
        value["attachment_refs"][identity.index],
        max_bytes=settings.attachment_max_bytes,
        deadline=deadline,
    )
