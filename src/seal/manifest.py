"""Bounded run evidence: a handled queue never establishes site completeness."""

from .objects import Objects


def pending_inputs(c, run_id, captures, *, verify=False):
    """Safe evidence only; a committed delivery is never an unreturned input."""
    row = c.execute("SELECT report FROM seal.run WHERE id=%s", (run_id,)).fetchone()
    inputs = dict(row["report"].get("undelivered_inputs", {}))
    fields = ("body_hash", "archive_ref", "status", "actual_url", "input_mode", "error")
    for url, capture in captures.items():
        previous = inputs.get(url, {})
        files = set(previous.get("attachment_refs", [])) | set(capture.get("attachment_refs", []))
        if capture.get("archive_ref"):
            files.add(capture["archive_ref"])
        inputs[url] = {key: capture.get(key) for key in fields}
        inputs[url]["attachment_refs"] = sorted(files)
    delivered = {
        r["url"] for r in c.execute("SELECT url FROM seal.observation WHERE run_id=%s", (run_id,))
    }
    inputs = {
        url: {key: capture.get(key) for key in (*fields, "attachment_refs")}
        for url, capture in inputs.items()
        if url not in delivered
    }
    if verify:
        objects = Objects()
        for capture in inputs.values():
            for ref in capture["attachment_refs"]:
                objects.get(ref)
    return inputs


def terminal_report(c, source, run, status, reason):
    from .progress import snapshot

    errors = sorted(set(run["errors"]) | ({reason} if reason else set()))
    report = dict(
        run["report"],
        errors=errors,
        counts=snapshot(c, run).counts,
        scope_evidence={
            "site_population": "unknown",
            "unknown_coverage": True,
            "absence_semantics": "unobserved_is_unknown_never_deleted",
        },
        quality_status="not_evaluated",
    )
    frontier = c.execute(
        "SELECT checkpoint FROM seal.frontier WHERE run_id=%s", (run["id"],)
    ).fetchone()
    captures = frontier["checkpoint"].get("acquired", {}) if frontier else {}
    # These are references already committed in the verified checkpoint. Recording
    # failure must remain possible during an object-store outage. A later Source
    # checkpoint replacement revalidates them before copying/clearing the evidence.
    pending = pending_inputs(c, run["id"], captures)
    report.pop("undelivered_inputs", None)
    if pending:
        report["undelivered_inputs"] = pending
    return report
