"""Read-only discovery progress and archived network counters."""

from datetime import date, datetime

from .manifest import has_table
from .run_state import TERMINAL_STATES

# Bounds metadata even when a recipe emits an unbounded stream of duplicate URLs.
EVENT_LIMIT = 100_000
DOWNLOAD_FAILURES = {
    "download_failed",
    "TimeoutError",
    "DNSLookupError",
    "TCPTimedOutError",
    "ConnectionRefusedError",
    "ConnectionDone",
    "ConnectionLost",
    "ResponseNeverReceived",
    "TunnelError",
}


def network_counts(c, run):
    """Keep network attempts distinct from responses admitted to the archive."""
    counts = c.execute(
        """SELECT count(*) AS http_attempts,
                  count(snapshot_id) AS archived_observations
           FROM seal_fetch_observation WHERE run_id=%s AND attempt_epoch=%s
             AND origin='network'""",
        (run["id"], run["attempt_epoch"]),
    ).fetchone()
    # Sensitive bodies are rejected before an Observation is persisted. Native
    # downloader stats still count that exchange, without retaining its bytes.
    stats = run.get("report", {}).get("stats")
    counts["http_attempts_basis"] = "observations_lower_bound"
    if run.get("mode") == "replay":
        counts.update(
            http_attempts=0, archived_observations=0, http_attempts_basis="offline_replay"
        )
    elif stats is not None:
        counts.update(
            http_attempts=stats.get("downloader/request_count", 0),
            http_attempts_basis="scrapy_downloader",
        )
    return counts


def summarize_discovery(c, run, include_events=False):
    """Count discovery lifecycle separately from persisted HTTP observations.

    ``requested`` means a request passed Guard (or was restored by Replay), while
    ``http_attempts`` uses native downloader stats, including refused bodies;
    without completed stats, persisted observations are only a lower bound.
    ``parsed`` requires the final successful state, including valid zero-Record
    callbacks. ``callback_completed`` also counts callbacks whose later item
    validation failed, so a callback return cannot imply a valid structured result.
    """
    available = has_table(c, "seal_discovery")
    rows = (
        c.execute(
            "SELECT * FROM seal_discovery WHERE run_id=%s AND attempt_epoch=%s ORDER BY created_at,id",
            (run["id"], run["attempt_epoch"]),
        ).fetchall()
        if available
        else []
    )
    outcomes = {}
    unknown = set()
    instrumented = available
    if (
        available
        and not rows
        and run.get("status") in TERMINAL_STATES
        and not run.get("report", {}).get("discovery", {}).get("instrumented")
    ):
        # Installing a table cannot retrospectively make historical zero evidence
        # a measured zero. Completion writes instrumented=True for new attempts.
        instrumented = False
        unknown.add("discovery_not_recorded")
    # A failed transport attempt remains visible, but a successful final callback resolves
    # its coverage uncertainty. Lineage is used only for evidence, never for deduplication.
    by_id = {row["id"]: row for row in rows}

    def recovered(row):
        seen = set()
        while row.get("continued_by") and row["id"] not in seen:
            seen.add(row["id"])
            row = by_id.get(row["continued_by"])
            if row is None:
                return False
        return row["state"] == "parsed"

    for row in rows:
        reason = row["reason"]
        if reason:
            outcomes[reason] = outcomes.get(reason, 0) + 1
        expected_scope = row.get("scope_decision") == "rejected" and reason in {
            "request_out_of_scope",
            "request_excluded",
            "iframe_out_of_scope",
            "attachment_host_path_required",
        }
        if (
            row["state"] in ("failed", "skipped")
            and reason not in {"duplicate_request", "optional_entry_missing"}
            and not expected_scope
        ):
            if not recovered(row):
                status = row["response_status"]
                if status == 429:
                    unknown.add("source_rate_limited")
                elif status is not None and status >= 500:
                    unknown.add("http_5xx")
                elif status is not None and status >= 400:
                    unknown.add("http_error")
                elif reason in DOWNLOAD_FAILURES and row["archived_at"] is None:
                    unknown.add("download_failed")
                else:
                    unknown.add(reason or "unresolved_discovery")
        elif row["state"] in ("discovered", "requested", "archived"):
            unknown.add("unresolved_discovery")
    if "discovery_budget_exceeded" in run.get("errors", []):
        unknown.add("discovery_budget_exceeded")
    observations = network_counts(c, run)
    summary = {
        "contract": 1,
        "instrumented": instrumented,
        "discovered": len(rows),
        "requested": sum(row["requested_at"] is not None for row in rows),
        "archived": sum(row["archived_at"] is not None for row in rows),
        "parsed": sum(row["state"] == "parsed" for row in rows),
        "callback_completed": sum(row["parsed_at"] is not None for row in rows),
        "failed": sum(row["state"] == "failed" for row in rows),
        "skipped": sum(row["state"] == "skipped" for row in rows),
        "deduplicated": sum(row["reason"] == "duplicate_request" for row in rows),
        "unique_fingerprints": len({row["fingerprint"] for row in rows}),
        **observations,
        "replayed": sum(row["replayed"] for row in rows),
        "pending": sum(row["state"] in ("discovered", "requested", "archived") for row in rows),
        "unknown_coverage": bool(unknown),
        "unknown_coverage_reasons": sorted(unknown),
        "outcomes": outcomes,
        "event_limit": EVENT_LIMIT,
    }
    if include_events:
        summary["events"] = [
            {
                key: value.isoformat() if isinstance(value, (date, datetime)) else value
                for key, value in row.items()
            }
            for row in rows
        ]
    return summary
