"""Finalize the operational Run; each valid resource response commits independently."""

from .db import connect, fenced, locked_run
from .run_state import TERMINAL_STATES


def finish_run(run_id, epoch):
    from .runs import end_run, receipt

    with connect() as c:
        source, run = locked_run(c, run_id)
        if run["status"] not in TERMINAL_STATES:
            fenced(source, run, epoch)
            end_run(c, source, run, "failed" if run["errors"] else "complete", None)
    return receipt(run_id)
