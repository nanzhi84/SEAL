"""Freeze due Resources (stable identity, display URL, recheck locator, current Version)."""


def plan_recheck(c, source, config):
    rows = c.execute(
        """SELECT r.id,r.identity_namespace AS namespace,r.external_key,r.public_url,
        r.recheck_locator,r.current_version_id FROM seal.resource r
        JOIN seal.frontier f ON f.id=r.frontier_id
        WHERE f.source_id=%s AND r.next_check<=now() ORDER BY r.next_check,r.id LIMIT %s""",
        (source["id"], config["budget"]["documents"] + 1),
    ).fetchall()
    selected = rows[: config["budget"]["documents"]]
    return {
        "selection": "due_resources",
        "resource_count": len(selected),
        "truncated": len(rows) > len(selected),
        "resources": selected,
    }
