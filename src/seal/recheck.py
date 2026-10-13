"""Freeze due known URLs, including resources no longer linked from the entry page."""

from .core import SealError


def plan_recheck(c, source, config):
    rows = c.execute(
        """SELECT r.url FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id
      WHERE f.source_id=%s AND r.role='content' AND r.next_check<=now() ORDER BY r.next_check,r.url""",
        (source["id"],),
    ).fetchall()
    if len(rows) > config["budget"]["requests"]:
        raise SealError("recheck_budget_exceeded")
    return [{"url": row["url"]} for row in rows], {
        "selection": "due_resources",
        "resource_count": len(rows),
    }
