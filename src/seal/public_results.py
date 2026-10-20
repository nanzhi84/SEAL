"""Unified read boundary: a fixed Snapshot's 16-column rows and downloadable file closure.

The API reads fixed rows and authorized file references through this boundary.
"""

from .document import COLUMNS, row_values

ROW_QUERY = """WITH base AS (
SELECT i.source_id,i.resource_id,d.*,
CASE WHEN d.volume_order IS NULL THEN left(d.markdown_file,-3)
ELSE regexp_replace(left(d.markdown_file,-3),'_[0-9]+$','') END AS name_stem
FROM seal.snapshot_item i
JOIN seal.delivery_row d ON d.version_id=i.version_id
WHERE i.snapshot_id=%(snapshot)s), named AS (
SELECT *,row_number() OVER (PARTITION BY markdown_file
ORDER BY source_id COLLATE "C",resource_id COLLATE "C",row_number) AS name_rank,
row_number() OVER (PARTITION BY name_stem
ORDER BY source_id COLLATE "C",resource_id COLLATE "C",row_number) AS stem_rank,
max(COALESCE(volume_order,0)) OVER (PARTITION BY name_stem) AS reserved_volume
FROM base)
SELECT * FROM named WHERE source_id = ANY(%(sources)s)
AND (source_id COLLATE "C", resource_id COLLATE "C", row_number)
    > (%(source_after)s COLLATE "C", %(resource_after)s COLLATE "C", %(row_after)s)
ORDER BY source_id COLLATE "C", resource_id COLLATE "C", row_number
LIMIT %(limit)s"""


def snapshot_rows(c, snapshot_id, sources, after=("", "", 0), limit=1000):
    """Stable row order: source_id, resource_id, row_number (C collation)."""
    rows = c.execute(
        ROW_QUERY,
        {
            "snapshot": snapshot_id,
            "sources": list(sources),
            "source_after": after[0],
            "resource_after": after[1],
            "row_after": after[2],
            "limit": limit,
        },
    ).fetchall()
    for row in rows:
        if row["name_rank"] > 1:
            stem = row["name_stem"] + "_%02d" % (row["reserved_volume"] + row["stem_rank"])
            row["markdown_file"] = stem + ".md"
            if row["original_file"]:
                row["original_file"] = stem + "." + row["original_file"].rsplit(".", 1)[1]
    return rows


def project(row):
    """One public API item: the 16 template columns plus addressing identifiers."""
    values = row_values(row)
    return {
        **{column: values[column] for column in COLUMNS},
        "source_id": row["source_id"],
        "resource_id": row["resource_id"],
        "version_id": row["version_id"],
        "row_number": row["row_number"],
        "markdown_hash": row["markdown_hash"],
    }


def file_entry(c, snapshot_id, sha256, sources):
    """A downloadable file of the published Snapshot within the caller's Sources."""
    return c.execute(
        """SELECT sha256,kind,media_type,file_name,byte_size,source_id FROM seal.snapshot_file
        WHERE snapshot_id=%s AND sha256=%s AND source_id = ANY(%s)
        ORDER BY kind DESC, source_id COLLATE "C" LIMIT 1""",
        (snapshot_id, sha256, list(sources)),
    ).fetchone()
