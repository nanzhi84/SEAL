"""Source-local URL identity and immutable, transactional response timelines."""

from importlib.metadata import version

from .archive import verify_result
from .core import SealError, public_url, uid
from .db import connect, fenced, j, locked_run, one
from .objects import Objects


def resource(c, frontier_id, url, role="content"):
    url = public_url(url)
    c.execute(
        "INSERT INTO seal.resource(id,frontier_id,url,role) VALUES(%s,%s,%s,%s) ON CONFLICT(frontier_id,url) DO NOTHING",
        (uid(), frontier_id, url, role),
    )
    return one(
        c,
        "SELECT * FROM seal.resource WHERE frontier_id=%s AND url=%s FOR UPDATE",
        (frontier_id, url),
    )


def persist(
    run_id,
    epoch,
    url,
    capture,
    outcome,
    error=None,
    role="content",
    *,
    result_ref=None,
):
    """One run/URL delivery commits once, even after checkpoint replay or worker takeover."""
    body_hash = capture.get("body_hash")
    archive_ref = capture.get("archive_ref")
    files = set(capture.get("attachment_refs", []))
    if archive_ref:
        files.add(archive_ref)
    if result_ref:
        envelope = verify_result(Objects(), result_ref)
        files.update(envelope.get("attachment_refs", []))
    for ref in files:
        Objects().get(ref)  # Original attachments, never a textual input fingerprint.
    with connect() as c:
        source, run = locked_run(c, run_id)
        fenced(source, run, epoch)
        frontier = one(c, "SELECT * FROM seal.frontier WHERE source_id=%s", (source["id"],))
        r = resource(c, frontier["id"], url, role)
        key = run_id + ":" + r["id"]
        existing = c.execute(
            "SELECT id FROM seal.observation WHERE delivery_key=%s", (key,)
        ).fetchone()
        if existing:
            return existing["id"]
        input_mode = capture.get("input_mode", run["config"]["crawl4ai"]["mode"])
        current = c.execute(
            "SELECT * FROM seal.version WHERE id=%s", (r["current_version_id"],)
        ).fetchone()
        version_id = None
        new = False
        if outcome == "parsed":
            if not body_hash or not result_ref:
                raise SealError("parsed_response_evidence_required")
            if current and current["input_mode"] != input_mode:
                raise SealError("input_mode_change_requires_new_source")
            new = current is None or current["body_hash"] != body_hash
            contract = (envelope["result"].get("metadata") or {}).get("pdf_text_contract")
            mime = (
                next(
                    (
                        v
                        for k, v in capture.get("headers", {}).items()
                        if k.lower() == "content-type"
                    ),
                    "",
                )
                .split(";")[0]
                .strip()
                .lower()
            )
            if not new and contract and mime == "application/pdf":
                previous = verify_result(Objects(), current["result_ref"])
                previous_contract = (previous["result"].get("metadata") or {}).get(
                    "pdf_text_contract"
                )
                # A changed text contract is a new immutable representation of the same
                # input. Runtime timestamps never participate in version identity.
                new = contract != previous_contract
            version_id = uid() if new else current["id"]
            if not new:
                outcome = "unchanged"
        oid = uid()
        c.execute(
            """INSERT INTO seal.observation(id,resource_id,run_id,attempt_epoch,delivery_key,
          url,actual_url,status,body_hash,archive_ref,result_ref,input_mode,outcome,error,version_id,attachment_refs)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                oid,
                r["id"],
                run_id,
                epoch,
                key,
                r["url"],
                capture.get("actual_url"),
                capture.get("status"),
                body_hash,
                archive_ref,
                result_ref,
                input_mode,
                outcome,
                error,
                version_id,
                sorted(files),
            ),
        )
        if new:
            # Serialize cursor allocation through COMMIT. A downstream cursor can never skip
            # an earlier uncommitted version from a different Source.
            c.execute("SELECT pg_advisory_xact_lock(72032102)")
            c.execute(
                """INSERT INTO seal.version(id,resource_id,sequence,observation_id,body_hash,
              input_mode,result_ref,framework_version,parser_config) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    version_id,
                    r["id"],
                    current["sequence"] + 1 if current else 1,
                    oid,
                    body_hash,
                    input_mode,
                    result_ref,
                    version("crawl4ai"),
                    j(run["config"]["crawl4ai"]),
                ),
            )
            c.execute(
                "UPDATE seal.resource SET current_version_id=%s WHERE id=%s", (version_id, r["id"])
            )
        interval = (
            run["config"]["recheck_seconds"]
            if version_id
            else min(60, run["config"]["recheck_seconds"])
        )
        c.execute(
            """UPDATE seal.resource SET last_checked_at=now(),last_outcome=%s,
          next_check=now()+(%s * interval '1 second') WHERE id=%s""",
            (outcome, interval, r["id"]),
        )
        if error:
            c.execute("UPDATE seal.run SET errors=errors || %s WHERE id=%s", (j([error]), run_id))
        return oid


def save_checkpoint(run_id, epoch, checkpoint):
    with connect() as c:
        source, run = locked_run(c, run_id)
        fenced(source, run, epoch)
        c.execute(
            "UPDATE seal.frontier SET checkpoint=%s,updated_at=now() WHERE source_id=%s AND run_id=%s",
            (j(checkpoint), source["id"], run_id),
        )


def delivered(run_id):
    with connect() as c:
        return {
            r["url"]
            for r in c.execute("SELECT url FROM seal.observation WHERE run_id=%s", (run_id,))
        }
