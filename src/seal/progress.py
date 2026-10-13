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
    counts = c.execute(
        """SELECT count(*) FILTER(WHERE r.role='content') AS fetched,
      count(*) FILTER(WHERE r.role='content') AS processed,
      count(*) FILTER(WHERE r.role='content' AND o.version_id IS NOT NULL) AS resources,
      count(*) FILTER(WHERE r.role='content' AND o.version_id IS NULL) AS failed,
      count(*) FILTER(WHERE o.outcome='unchanged') AS unchanged,
      count(*) FILTER(WHERE o.outcome='parsed') AS updated
      FROM seal.observation o JOIN seal.resource r ON r.id=o.resource_id WHERE o.run_id=%s""",
        (run["id"],),
    ).fetchone()
    frontier = c.execute(
        "SELECT checkpoint FROM seal.frontier WHERE source_id=%s AND run_id=%s",
        (run["source_id"], run["id"]),
    ).fetchone()
    pending = frontier["checkpoint"].get("inflight", []) if frontier else []
    counts.update(
        discovered=counts["fetched"] + len(pending),
        pending=len(pending),
        active=len(pending),
        resources=counts["resources"],
    )
    return ProgressSnapshot(
        run["id"],
        run["source_id"],
        run["mode"],
        run["status"],
        run["created_at"].isoformat(),
        counts,
        safe_url(pending[-1])[:160] if pending else None,
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
            Text(f"Resources {counts['resources']:,}"),
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
