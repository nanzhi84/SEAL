"""Four operator commands with one JSON receipt and optional stderr progress."""

import argparse
import json
import sys
from pathlib import Path

from .core import SealError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SealError("invalid_arguments")


def parser():
    root = Parser(prog="seal")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Explicitly initialize or upgrade storage")
    init.add_argument("--backup", type=Path)
    init.add_argument("--acknowledge-data-loss", action="store_true")
    for mode in ("collect", "recheck"):
        command = commands.add_parser(mode, help="Online " + mode + " from a Source YAML")
        command.add_argument("source", type=Path)
        command.add_argument("--enqueue", action="store_true")
        command.add_argument("--no-progress", action="store_true")
    commands.add_parser("worker", help="Consume persistent jobs").add_argument(
        "--once", action="store_true"
    )
    return root


def dispatch(args):
    from .migration import initialize, require_current

    if args.command == "init":
        return initialize(backup=args.backup, acknowledge=args.acknowledge_data_loss)
    require_current()
    if args.command == "worker":
        from .queue import worker

        worker(args.once)
        return {"worker": "stopped"}
    from .configuration import prepare_run
    from .progress import ProgressPanel
    from .runs import execute_run, receipt

    run_id = prepare_run(args.source, args.command, enqueue=args.enqueue)
    if args.enqueue:
        return receipt(run_id)
    with ProgressPanel(not args.no_progress) as panel:
        try:
            result = execute_run(run_id, progress=panel)
        except KeyboardInterrupt:
            from .db import connect, j, locked_run

            with connect() as c:
                _, run = locked_run(c, run_id)
                if run["status"] in {"running", "finishing"}:
                    c.execute(
                        "UPDATE seal.run SET status='retryable',errors=errors || %s WHERE id=%s",
                        (j(["interrupted"]), run_id),
                    )
            result = dict(receipt(run_id), error="interrupted")
        panel.update(run_id, final=True)
        return result


def main():
    try:
        args = parser().parse_args()
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, default=str))
        if result.get("error") == "interrupted":
            return 130
        if result.get("error"):
            return 2
        return 1 if result.get("status") in {"failed", "retryable"} else 0
    except SealError as exc:
        print(json.dumps({"error": exc.code}))
        return 2
    except Exception as exc:
        print(json.dumps({"error": "operation_failed", "category": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
