"""Freeze an explicit offline reparse input set: current Versions' archived inputs only."""


def plan_reparse(c, source, config):
    rows = c.execute(
        """SELECT r.id AS resource_id,r.identity_namespace AS namespace,r.external_key,
        r.public_url,r.recheck_locator,v.id AS version_id,v.inputs_ref,v.parser_fingerprint
        FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id
        JOIN seal.version v ON v.id=r.current_version_id
        WHERE f.source_id=%s ORDER BY r.id LIMIT %s""",
        (source["id"], config["budget"]["documents"] + 1),
    ).fetchall()
    selected = rows[: config["budget"]["documents"]]
    return {
        "selection": "current_versions",
        "network": "disabled",
        "resource_count": len(selected),
        "truncated": len(rows) > len(selected),
        "resources": selected,
    }
