"""Read-only current-attempt progress; URLs, HTTP responses and documents differ."""

import time
from dataclasses import dataclass

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from .core import safe_url
from .db import connect, one


@dataclass(frozen=True)
class ProgressSnapshot:
    run_id: str
    source_id: str
    mode: str
    status: str
    started_at: str
    counts: dict
    current_url: str | None


def snapshot(c, run):
    # A URL is handled only when all currently discovered representations and
    # transports have terminal outcomes. Discovered includes duplicate candidates
    # only once. Recovered retries and intermediate redirects are not failures.
    counts = c.execute(
        """WITH urls AS (
        SELECT url,bool_and(state IN ('parsed','failed','skipped')) AS handled,
          bool_or(state='failed' AND continued_by IS NULL) AS failed
        FROM seal.discovery WHERE run_id=%s AND attempt_epoch=%s AND role NOT IN ('robots','sitemap')
        GROUP BY url)
        SELECT count(*) AS discovered,count(*) FILTER(WHERE handled) AS processed,
          count(*) FILTER(WHERE failed) AS failed FROM urls""",
        (run["id"], run["attempt_epoch"]),
    ).fetchone()
    states = c.execute(
        """SELECT count(*) FILTER(WHERE state='discovered') AS pending,
        count(*) FILTER(WHERE state IN ('requested','archived')) AS active
        FROM seal.discovery WHERE run_id=%s AND attempt_epoch=%s""",
        (run["id"], run["attempt_epoch"]),
    ).fetchone()
    fetched = c.execute(
        "SELECT count(*) AS fetched FROM seal.fetch_observation WHERE run_id=%s AND attempt_epoch=%s AND is_content",
        (run["id"], run["attempt_epoch"]),
    ).fetchone()
    docs = c.execute(
        """SELECT count(DISTINCT document_id) AS documents,
        count(DISTINCT document_id) FILTER(WHERE change_kind='unchanged') AS unchanged,
        count(DISTINCT document_id) FILTER(WHERE change_kind='updated') AS updated
        FROM seal.document_emission WHERE run_id=%s AND attempt_epoch=%s""",
        (run["id"], run["attempt_epoch"]),
    ).fetchone()
    current = c.execute(
        """SELECT url FROM seal.discovery WHERE run_id=%s AND attempt_epoch=%s
        AND state IN ('requested','archived') ORDER BY requested_at DESC NULLS LAST LIMIT 1""",
        (run["id"], run["attempt_epoch"]),
    ).fetchone()
    url = safe_url(current["url"])[:160] if current else None
    return ProgressSnapshot(
        run["id"],
        run["source_id"],
        run["mode"],
        run["status"],
        run["created_at"].isoformat(),
        {**counts, **states, **fetched, **docs},
        url,
    )


class ProgressPanel:
    def __init__(self, enabled):
        self.console = Console(stderr=True)
        self.enabled = enabled and self.console.is_terminal
        self.live = None
        self.started = time.monotonic()
        self.sampled = 0.0

    def __enter__(self):
        if self.enabled:
            try:
                self.live = Live(
                    Text("SEAL Initializing"),
                    console=self.console,
                    refresh_per_second=4,
                    transient=False,
                )
                self.live.start()
            except Exception:
                self.live = None
        return self

    def update(self, run_id, *, final=False):
        if not self.live or (not final and time.monotonic() - self.sampled < 1):
            return
        self.sampled = time.monotonic()
        try:
            with connect() as c:
                state = snapshot(c, one(c, "SELECT * FROM seal.run WHERE id=%s", (run_id,)))
            self.live.update(self.render(state))
        except Exception:
            # Display is never a prerequisite for correct execution.
            self.stop()
            self.live = None

    def render(self, state):
        table = Table.grid(padding=(0, 2))
        elapsed = int(time.monotonic() - self.started)
        table.add_row(
            Text("SEAL " + state.mode.title(), style="bold cyan"),
            Text(state.status),
            Text(f"{elapsed // 3600:02}:{elapsed // 60 % 60:02}:{elapsed % 60:02}"),
        )
        table.add_row(Text("Source " + state.source_id), Text(state.run_id[:8]))
        counts = state.counts
        table.add_row(
            Text(f"Discovered {counts['discovered']:,}"),
            Text(f"Fetched {counts['fetched']:,}"),
            Text(f"Documents {counts['documents']:,}"),
        )
        table.add_row(
            Text(f"Failed {counts['failed']:,}"),
            Text(f"Pending {counts['pending']:,}"),
            Text(f"Active {counts['active']:,}"),
        )
        if state.mode == "recheck":
            table.add_row(
                Text(f"Unchanged {counts['unchanged']:,}"), Text(f"Updated {counts['updated']:,}")
            )
        total, done = counts["discovered"], counts["processed"]
        filled = int(16 * done / total) if total else 0
        table.add_row(
            Text("Known URL progress"),
            Text("[" + "█" * filled + "░" * (16 - filled) + "]"),
            Text(f"{done} / {total}"),
        )
        if state.current_url:
            table.add_row(Text("Current"), Text(state.current_url, overflow="ellipsis"))
        return table

    def __exit__(self, *args):
        self.stop()

    def stop(self):
        if self.live:
            try:
                self.live.stop()
            except Exception:
                pass  # Terminal teardown cannot change the persisted Run outcome.
