"""Explicit immutable global captures and serialized operator publication."""

import json
import time
from uuid import UUID

from .archive import verify_result
from .core import MAX_ORIGINAL_BYTES, SealError, uid
from .db import connect, j, one
from .objects import Objects
from .snapshot_overrides import normalize

OUTPUT_MAX_BYTES = 8 * 1024 * 1024


def identity(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SealError("invalid_snapshot_id") from None
    return value


def bounded(value):
    if len(json.dumps(value, ensure_ascii=False, default=str).encode()) > OUTPUT_MAX_BYTES:
        raise SealError("snapshot_output_too_large")
    return value


def header(c, snapshot_id):
    value = c.execute(
        "SELECT * FROM seal.snapshot WHERE id=%s AND sealed", (identity(snapshot_id),)
    ).fetchone()
    if value is None:
        raise SealError("snapshot_not_found")
    return value


def published(c):
    return one(c, "SELECT published_snapshot_id FROM seal.publication WHERE singleton")[
        "published_snapshot_id"
    ]


def create(overrides=None):
    overrides = normalize([] if overrides is None else overrides)
    snapshot_id = uid()
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        c.execute("INSERT INTO seal.snapshot(id) VALUES(%s)", (snapshot_id,))
        c.execute(
            """INSERT INTO seal.snapshot_source(snapshot_id,source_id,frontier_id,entry_urls)
            SELECT %s,s.id,f.id,s.config->'entry_urls'
            FROM seal.source s JOIN seal.frontier f ON f.source_id=s.id""",
            (snapshot_id,),
        )
        # Validate the complete override list against this same MVCC view before
        # inserting mappings. Never UPDATE immutable snapshot_item rows.
        selected = c.execute(
            """SELECT o.resource_id,o.version_id,v.result_ref,
              r.id IS NOT NULL AS resource_exists,
              v.id IS NOT NULL AND v.resource_id=r.id AS version_matches,
              s.source_id IS NOT NULL AS source_matches
            FROM jsonb_to_recordset(%s::jsonb) AS o(resource_id text,version_id text)
            LEFT JOIN seal.resource r ON r.id=o.resource_id
            LEFT JOIN seal.version v ON v.id=o.version_id
            LEFT JOIN seal.snapshot_source s ON s.snapshot_id=%s AND s.frontier_id=r.frontier_id""",
            (j(overrides), snapshot_id),
        ).fetchall()
        deadline = time.monotonic() + 60
        objects = Objects() if selected else None
        for item in selected:
            if not all(item[k] for k in ("resource_exists", "version_matches", "source_matches")):
                raise SealError("invalid_override_version")
            verify_result(
                objects,
                item["result_ref"],
                max_bytes=64 * 1024 * 1024,
                attachment_max_bytes=MAX_ORIGINAL_BYTES,
                deadline=deadline,
            )
        c.execute(
            """INSERT INTO seal.snapshot_item
            (snapshot_id,source_id,frontier_id,resource_id,version_id,url)
            SELECT s.snapshot_id,s.source_id,s.frontier_id,r.id,v.id,r.url
            FROM seal.snapshot_source s JOIN seal.resource r ON r.frontier_id=s.frontier_id
            LEFT JOIN jsonb_to_recordset(%s::jsonb) AS o(resource_id text,version_id text)
              ON o.resource_id=r.id
            JOIN seal.version v ON v.id=COALESCE(o.version_id,r.current_version_id)
              AND v.resource_id=r.id
            WHERE s.snapshot_id=%s""",
            (j(overrides), snapshot_id),
        )
        row = c.execute(
            """UPDATE seal.snapshot SET sealed=true,
            source_count=(SELECT count(*) FROM seal.snapshot_source WHERE snapshot_id=%s),
            resource_count=(SELECT count(*) FROM seal.snapshot_item WHERE snapshot_id=%s),
            version_count=(SELECT count(DISTINCT version_id) FROM seal.snapshot_item
                           WHERE snapshot_id=%s)
            WHERE id=%s RETURNING *""",
            (snapshot_id,) * 4,
        ).fetchone()
        pointer = published(c)
    return dict(row, snapshot_id=snapshot_id, published_snapshot_id=pointer)


def pagination(limit, after):
    if not 1 <= limit <= 100 or len(after) > 1000:
        raise SealError("invalid_arguments")


def listing(limit=20, after=""):
    pagination(limit, after)
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        rows = c.execute(
            """SELECT * FROM seal.snapshot WHERE sealed AND id COLLATE "C">%s COLLATE "C"
            ORDER BY id COLLATE "C" LIMIT %s""",
            (after, limit + 1),
        ).fetchall()
        pointer = published(c)
    return bounded(
        {
            "snapshots": rows[:limit],
            "next_after": rows[limit - 1]["id"] if len(rows) > limit else None,
            "published_snapshot_id": pointer,
        }
    )


def inspect(snapshot_id, limit=20, after="", source_after=""):
    pagination(limit, after)
    pagination(limit, source_after)
    with connect() as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        row = header(c, snapshot_id)
        items = c.execute(
            """SELECT source_id,resource_id,version_id,url FROM seal.snapshot_item
            WHERE snapshot_id=%s AND resource_id COLLATE "C">%s COLLATE "C"
            ORDER BY resource_id COLLATE "C" LIMIT %s""",
            (snapshot_id, after, limit + 1),
        ).fetchall()
        sources = c.execute(
            """SELECT source_id,entry_urls FROM seal.snapshot_source
            WHERE snapshot_id=%s AND source_id COLLATE "C">%s COLLATE "C"
            ORDER BY source_id COLLATE "C" LIMIT %s""",
            (snapshot_id, source_after, limit + 1),
        ).fetchall()
        pointer = published(c)
    return bounded(
        dict(
            row,
            snapshot_id=snapshot_id,
            published_snapshot_id=pointer,
            items=items[:limit],
            sources=sources[:limit],
            next_after=items[limit - 1]["resource_id"] if len(items) > limit else None,
            source_next_after=sources[limit - 1]["source_id"] if len(sources) > limit else None,
        )
    )


def verify(snapshot_id):
    """Bounded batches, read existing archives only; never certify business quality."""
    objects, after = Objects(), ""
    with connect() as c:
        c.execute("SET TRANSACTION READ ONLY")
        header(c, snapshot_id)
        while True:
            rows = c.execute(
                """SELECT i.resource_id,v.result_ref FROM seal.snapshot_item i
                JOIN seal.version v ON v.id=i.version_id AND v.resource_id=i.resource_id
                WHERE i.snapshot_id=%s AND i.resource_id COLLATE "C">%s COLLATE "C"
                ORDER BY i.resource_id COLLATE "C" LIMIT 100""",
                (snapshot_id, after),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                verify_result(
                    objects,
                    row["result_ref"],
                    max_bytes=64 * 1024 * 1024,
                    attachment_max_bytes=MAX_ORIGINAL_BYTES,
                    deadline=time.monotonic() + 60,
                )
            after = rows[-1]["resource_id"]


def publish(snapshot_id):
    verify(identity(snapshot_id))
    with connect() as c:
        pointer = one(c, "SELECT * FROM seal.publication WHERE singleton FOR UPDATE")
        before = pointer["published_snapshot_id"]
        changed = before != snapshot_id
        if changed:
            c.execute(
                "UPDATE seal.publication SET published_snapshot_id=%s WHERE singleton",
                (snapshot_id,),
            )
            record = c.execute(
                """INSERT INTO seal.snapshot_publication(before_snapshot_id,after_snapshot_id)
                VALUES(%s,%s) RETURNING sequence,published_at""",
                (before, snapshot_id),
            ).fetchone()
        else:
            record = one(
                c,
                "SELECT sequence,published_at FROM seal.snapshot_publication ORDER BY sequence DESC LIMIT 1",
            )
    return dict(
        record,
        before_snapshot_id=before,
        published_snapshot_id=snapshot_id,
        changed=changed,
    )
