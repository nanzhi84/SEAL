"""Run subprocess entrypoint and ownership watchdog."""

import asyncio
import os
import signal
import sys
import threading
import time

from .core import SealError
from .db import connect, record_error
from .parsing import require_runtime
from .runs import run_context
from .runtime import Runtime


def watch_parent(parent):
    while True:
        if os.getppid() != parent:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        time.sleep(0.2)


async def run(context):
    try:
        require_runtime(context["config"], context["parser_manifest"])
    except SealError as exc:
        record_error(context["id"], context["attempt_epoch"], exc.code)
        with connect() as c:
            c.execute(
                """UPDATE seal.run SET status='finishing' WHERE id=%s AND attempt_epoch=%s
                AND status='running'""",
                (context["id"], context["attempt_epoch"]),
            )
        return
    runtime = Runtime(context)
    task = asyncio.current_task()

    def cancel(signum, frame):
        runtime.budget.cancelled = True
        task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, cancel)
    await runtime.execute()


def main():
    run_id, epoch, parent = sys.argv[1:]
    threading.Thread(target=watch_parent, args=(int(parent),), daemon=True).start()
    asyncio.run(run(run_context(run_id, int(epoch))))


if __name__ == "__main__":
    main()
