"""Operator CLI. Successful processes and successful business runs are distinct."""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_file
from .core import SealError, atomic_write


def parser():
    root = argparse.ArgumentParser(prog="seal")
    commands = root.add_subparsers(dest="command", required=True)
    db = commands.add_parser("db").add_subparsers(dest="action", required=True)
    db.add_parser("migrate")
    source = commands.add_parser("source").add_subparsers(dest="action", required=True)
    source.add_parser("apply").add_argument("file", type=Path)
    select = source.add_parser("select")
    select.add_argument("source")
    select.add_argument("--binding", required=True)
    select.add_argument("--expect-generation", type=int, required=True)
    recipe = commands.add_parser("recipe").add_subparsers(dest="action", required=True)
    recipe.add_parser("pack").add_argument("path", type=Path)
    binding = (
        commands.add_parser("binding")
        .add_subparsers(dest="action", required=True)
        .add_parser("create")
    )
    binding.add_argument("source")
    binding.add_argument("--recipe", required=True)
    binding.add_argument("--params", required=True, type=Path)
    replay = commands.add_parser("replay")
    replay.add_argument("run")
    replay.add_argument("--binding")
    run = commands.add_parser("run")
    run.add_argument("source")
    run.add_argument("--binding")
    run.add_argument("--enqueue", action="store_true")
    run.add_argument("--recheck", action="store_true")
    commands.add_parser("worker").add_argument("--once", action="store_true")
    commands.add_parser("schedule")
    commands.add_parser("recover")
    commands.add_parser("finish").add_argument("run")
    retry = commands.add_parser("retry")
    retry.add_argument("run")
    retry.add_argument("--enqueue", action="store_true")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("kind", choices=["source", "binding", "run", "manifest", "research"])
    inspect.add_argument("identity")
    inspect.add_argument("--output", type=Path)
    export = commands.add_parser("export")
    export.add_argument("source")
    export.add_argument("--output", type=Path)
    export.add_argument("--run")
    pause = commands.add_parser("pause")
    pause.add_argument("source")
    pause.add_argument("--reason", required=True)
    return root


def dispatch(args):
    from . import configuration, recipes, runs
    from .completion import finish_run
    from .db import connect, decision, locked_run, migrate, one
    from .export import export_source
    from .inspect import inspect_record

    if args.command == "db":
        return migrate()
    if args.command == "source":
        if args.action == "select":
            return configuration.select_binding(args.source, args.binding, args.expect_generation)
        return configuration.register_source(load_file(args.file))
    if args.command == "recipe":
        return recipes.pack_recipe(args.path)
    if args.command == "binding":
        return recipes.create_binding(args.source, args.recipe, load_file(args.params))
    if args.command == "replay":
        with connect() as c:
            run = one(c, "SELECT * FROM seal_run WHERE id=%s", (args.run,))
        return runs.execute_run(
            runs.create_run(args.binding or run["binding_id"], "replay", replay_from=args.run)
        )
    if args.command == "run":
        with connect() as c:
            source = one(c, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (args.source,))
            binding_id = args.binding or source["binding_id"]
            if not binding_id:
                raise SealError("binding_required")
            binding = one(c, "SELECT source_id FROM seal_binding WHERE id=%s", (binding_id,))
            if binding["source_id"] != args.source:
                raise SealError("binding_source_mismatch")
            run_id = runs.create_run(binding_id, "recheck" if args.recheck else "collect", c)
            if args.enqueue:
                from .queue import enqueue

                enqueue(c, run_id)
        return runs.receipt(run_id) if args.enqueue else runs.execute_run(run_id)
    if args.command == "retry":
        with connect() as c:
            source, run = locked_run(c, args.run)
            if run["status"] not in ("retryable", "running", "finishing"):
                raise SealError("run_not_retryable")
            if run["attempt_epoch"] >= run["max_attempts"] or run["deadline"] <= datetime.now(
                timezone.utc
            ):
                raise SealError("attempts_or_deadline_exhausted")
            decision(c, source["id"], "control", {"action": "retry", "run_id": run["id"]})
            if args.enqueue:
                if run["job_id"]:
                    raise SealError("use_stalled_recovery_for_existing_job")
                from .queue import enqueue

                enqueue(c, args.run)
        return runs.receipt(args.run) if args.enqueue else runs.execute_run(args.run)
    if args.command == "finish":
        return finish_run(args.run)
    if args.command == "inspect":
        result = inspect_record(args.kind, args.identity)
        if args.output:
            write_json(args.output, result)
        return result
    if args.command == "export":
        result = export_source(args.source, args.run)
        if args.output:
            write_json(args.output, result)
        return result
    if args.command == "pause":
        return configuration.pause(args.source, args.reason)
    if args.command == "worker":
        from .queue import worker

        worker(args.once)
        return {"worker": "stopped"}
    if args.command == "schedule":
        from .queue import schedule_due

        return schedule_due()
    if args.command == "recover":
        from .queue import app, recover_stalled

        async def recover():
            async with app.open_async():
                return await recover_stalled()

        return asyncio.run(recover())
    raise SealError("unsupported_command")


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, default=str).encode())


def main():
    args = parser().parse_args()
    try:
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 1 if result.get("status") in ("partial", "failed", "superseded", "retryable") else 0
    except SealError as exc:
        print(json.dumps({"error": exc.code}))
        return 2
    except Exception as exc:
        # Error class is useful without potentially leaking secrets from framework text.
        print(json.dumps({"error": "operation_failed", "category": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
