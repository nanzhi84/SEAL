"""Explicit immutable global captures and serialized operator publication."""

import json
import time
from uuid import UUID

from .archive import verify_result
from .core import SealError, uid
from .db import connect, one
from .objects import Objects

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


def create():
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
        c.execute(
            """INSERT INTO seal.snapshot_item
            (snapshot_id,source_id,frontier_id,resource_id,version_id,url)
            SELECT s.snapshot_id,s.source_id,s.frontier_id,r.id,v.id,r.url
            FROM seal.snapshot_source s JOIN seal.resource r ON r.frontier_id=s.frontier_id
            JOIN seal.version v ON v.id=r.current_version_id AND v.resource_id=r.id
            WHERE s.snapshot_id=%s""",
            (snapshot_id,),
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
                    attachment_max_bytes=50 * 1024 * 1024,
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
