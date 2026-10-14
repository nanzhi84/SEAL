"""Read-only published snapshot selection and immutable capability pagination."""

import time
from urllib.parse import urlencode

from .api_security import ApiError, Attachment, Cursor
from .core import SealError, canonical
from .db import connect
from .objects import Objects
from .public_results import envelope, project


def published(c):
    pointer = c.execute(
        "SELECT published_snapshot_id FROM seal.publication WHERE singleton"
    ).fetchone()
    if pointer is None or pointer["published_snapshot_id"] is None:
        raise ApiError(503, "snapshot_not_published")
    return pointer["published_snapshot_id"]


def sources(settings, caller):
    """Advertise authorized frozen entries that resolve uniquely within the snapshot."""
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = published(c)
        rows = c.execute(
            """SELECT s.source_id AS id, jsonb_agg(e.url ORDER BY e.position) AS entry_urls
            FROM seal.snapshot_source s
            CROSS JOIN LATERAL (
                SELECT url, min(position) AS position
                FROM jsonb_array_elements_text(s.entry_urls)
                    WITH ORDINALITY AS entries(url,position)
                GROUP BY url
            ) e
            WHERE s.snapshot_id=%s AND s.source_id = ANY(%s::text[]) AND NOT EXISTS (
                SELECT 1 FROM seal.snapshot_source other
                WHERE other.snapshot_id=s.snapshot_id AND other.source_id <> s.source_id
                  AND other.entry_urls ? e.url
            )
            GROUP BY s.source_id ORDER BY s.source_id COLLATE "C"
            """,
            (snapshot, caller.sources),
        ).fetchall()
    body = canonical(rows)
    if len(body) > settings.page_max_bytes:
        raise SealError("archive_too_large")
    return body


def page_rows(tokens, source_url, caller, cursor_value, limit):
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        snapshot = published(c)
        cursor = tokens.decode(cursor_value, Cursor) if cursor_value is not None else None
        if cursor:
            if (
                cursor.caller != caller.id
                or not c.execute(
                    "SELECT 1 FROM seal.snapshot WHERE id=%s AND sealed", (cursor.snapshot,)
                ).fetchone()
            ):
                raise ApiError(400, "invalid_cursor")
            snapshot = cursor.snapshot
        matches = c.execute(
            """SELECT source_id FROM seal.snapshot_source
            WHERE snapshot_id=%s AND entry_urls ? %s ORDER BY source_id LIMIT 2""",
            (snapshot, source_url),
        ).fetchall()
        if not matches:
            raise ApiError(404, "source_not_found")
        if len(matches) != 1:
            raise ApiError(409, "source_ambiguous")
        source_id = matches[0]["source_id"]
        caller.authorize(source_id)
        if cursor and cursor.source != source_id:
            raise ApiError(400, "invalid_cursor")
        rows = c.execute(
            """SELECT i.resource_id,i.url,i.version_id,v.result_ref
            FROM seal.snapshot_item i JOIN seal.version v
              ON v.id=i.version_id AND v.resource_id=i.resource_id
            WHERE i.snapshot_id=%s AND i.source_id=%s
              AND i.resource_id COLLATE "C">%s COLLATE "C"
            ORDER BY i.resource_id COLLATE "C" LIMIT %s""",
            (snapshot, source_id, cursor.after if cursor else "", limit + 1),
        ).fetchall()
    return source_id, snapshot, cursor, rows


def results(settings, tokens, caller, source_url, limit, cursor_value, deadline):
    source_id, snapshot, cursor, rows = page_rows(tokens, source_url, caller, cursor_value, limit)
    objects, content, size = Objects(), [], 2
    for row in rows[:limit]:
        if time.monotonic() >= deadline:
            raise SealError("archive_read_timeout")
        row.update(source_id=source_id, snapshot_id=snapshot, caller_id=caller.id)
        value = envelope(
            objects, row["result_ref"], max_bytes=settings.result_max_bytes, deadline=deadline
        )
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
                snapshot=snapshot,
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


def attachment(settings, tokens, caller, token, deadline):
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        published(c)
        identity = tokens.decode(token, Attachment)
        if identity.caller != caller.id:
            raise ApiError(404, "attachment_not_found")
        caller.authorize(identity.source)
        row = c.execute(
            """SELECT v.result_ref FROM seal.snapshot_item i JOIN seal.version v
              ON v.id=i.version_id AND v.resource_id=i.resource_id
            WHERE i.snapshot_id=%s AND i.source_id=%s AND i.resource_id=%s AND i.version_id=%s""",
            (identity.snapshot, identity.source, identity.resource, identity.version),
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
