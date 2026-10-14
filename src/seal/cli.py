"""Operator commands and a read-only result server."""

import argparse
import json
import os
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
    serve = commands.add_parser("serve", help="Serve existing successful content and attachments")
    serve.add_argument("--config", type=Path, default=os.environ.get("SEAL_API_CONFIG"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    snapshot = commands.add_parser("snapshot", help="Capture, inspect and manually publish content")
    operations = snapshot.add_subparsers(dest="operation", required=True)
    operations.add_parser("create", help="Freeze global successful data without publishing")
    listing = operations.add_parser("list", help="List immutable captures")
    inspect = operations.add_parser("inspect", help="Inspect frozen sources and version mappings")
    inspect.add_argument("snapshot_id")
    inspect.add_argument("--source-after", default="")
    for paged in (listing, inspect):
        paged.add_argument("--limit", type=int, default=20)
        paged.add_argument("--after", default="")
    operations.add_parser("publish", help="Manually publish or restore a capture").add_argument(
        "snapshot_id"
    )
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
    if args.command == "serve":
        from .api import serve

        return serve(args.config, args.host, args.port)
    from .migration import initialize, require_current

    if args.command == "init":
        return initialize(backup=args.backup, acknowledge=args.acknowledge_data_loss)
    if args.command == "snapshot":
        from . import snapshots

        require_current()
        if args.operation == "create":
            return snapshots.create()
        if args.operation == "list":
            return snapshots.listing(args.limit, args.after)
        if args.operation == "inspect":
            return snapshots.inspect(args.snapshot_id, args.limit, args.after, args.source_after)
        return snapshots.publish(args.snapshot_id)
    from .objects import Objects

    Objects().check()
    require_current()
    if args.command == "worker":
        from .queue import worker

        return worker(args.once)
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
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted"}))
        return 130
    except SealError as exc:
        print(json.dumps({"error": exc.code}))
        return 2
    except Exception as exc:
        print(json.dumps({"error": "operation_failed", "category": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
