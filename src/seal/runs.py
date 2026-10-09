"""Run lifecycle and bounded subprocess execution shared by CLI and worker."""

import os
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone

from .core import SealError, uid
from .db import connect, j, locked_run, one


def create_run(binding_id, mode, connection=None, replay_from=None, slot=None):
    if mode not in ("collect", "recheck", "replay"):
        raise SealError("unsupported_run_mode")
    if connection is None:
        with connect() as c:
            return create_run(binding_id, mode, c, replay_from, slot)
    c = connection
    binding = one(c, "SELECT * FROM seal_binding WHERE id=%s", (binding_id,))
    source = one(c, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (binding["source_id"],))
    if slot:
        existing = c.execute("SELECT id FROM seal_run WHERE slot=%s", (slot,)).fetchone()
        if existing:
            return existing["id"]
    online = mode != "replay"
    if online and source["paused"]:
        raise SealError("source_paused")
    if (
        online
        and source["cooldown_until"]
        and source["cooldown_until"] > datetime.now(timezone.utc)
    ):
        raise SealError("source_cooling_down")
    identity = uid()
    config = binding["config"]
    seeds = [{"url": url, "role": config["seed_role"]} for url in config["entry_urls"]]
    replay_inputs = []
    if mode == "recheck":
        seeds = [
            {"url": d["url"], "role": "detail"}
            for d in c.execute(
                "SELECT url FROM seal_document WHERE source_id=%s AND namespace='runtime' ORDER BY identity",
                (source["id"],),
            ).fetchall()
        ]
        if not seeds:
            raise SealError("no_documents_to_recheck")
    if mode == "replay":
        original = one(c, "SELECT * FROM seal_run WHERE id=%s", (replay_from,))
        if original["source_id"] != source["id"]:
            raise SealError("cross_source_replay_rejected")
        if not original["inputs"]:
            raise SealError("replay_inputs_missing")
        seeds, replay_inputs = original["seeds"], original["inputs"]
    seq = source["run_seq"] + 1 if online else 0
    if online:
        c.execute("UPDATE seal_source SET run_seq=%s WHERE id=%s", (seq, source["id"]))
    c.execute(
        """INSERT INTO seal_run(id,source_id,binding_id,mode,namespace,generation,run_seq,deadline,seeds,replay_inputs,slot)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            identity,
            source["id"],
            binding_id,
            mode,
            "runtime" if online else mode + ":" + identity,
            source["generation"],
            seq,
            datetime.now(timezone.utc) + timedelta(seconds=config["budget"]["seconds"] * 3 + 120),
            j(seeds),
            j(replay_inputs),
            slot,
        ),
    )
    return identity


def begin_attempt(run_id):
    with connect() as c:
        source, run = locked_run(c, run_id)
        if run["status"] in ("complete", "superseded", "partial", "failed"):
            return None
        if run["attempt_epoch"] >= run["max_attempts"] or run["deadline"] <= datetime.now(
            timezone.utc
        ):
            c.execute(
                "UPDATE seal_run SET status='failed',completed_at=now(),errors=errors || %s WHERE id=%s",
                (j(["attempts_or_deadline_exhausted"]), run_id),
            )
            return None
        if run["mode"] not in ("collect", "recheck", "replay"):
            raise SealError("unsupported_run_mode")
        if run["mode"] != "replay":
            if (
                source["generation"] != run["generation"]
                or source["paused"]
                or source["write_seq"] > run["run_seq"]
            ):
                c.execute(
                    "UPDATE seal_run SET status='superseded',completed_at=now() WHERE id=%s",
                    (run_id,),
                )
                return None
            if source["cooldown_until"] and source["cooldown_until"] > datetime.now(timezone.utc):
                c.execute(
                    "UPDATE seal_run SET status='failed',errors=errors || %s,completed_at=now() WHERE id=%s",
                    (j(["source_cooling_down"]), run_id),
                )
                return None
            c.execute(
                "UPDATE seal_source SET write_seq=%s WHERE id=%s", (run["run_seq"], source["id"])
            )
        # Preserve prior attempt input/report history in the report object before reset.
        history = run["report"].get("attempts", [])
        if run["attempt_epoch"]:
            history += [
                {
                    "epoch": run["attempt_epoch"],
                    "inputs": run["inputs"],
                    "errors": run["errors"],
                    "results": run["result_ids"],
                }
            ]
        c.execute(
            "UPDATE seal_run SET attempt_epoch=attempt_epoch+1,status='running',inputs='[]',result_ids='[]',refs='[]',errors='[]',report=%s WHERE id=%s",
            (j({"attempts": history}), run_id),
        )
        return run["attempt_epoch"] + 1


def run_context(run_id, epoch):
    with connect() as c:
        run = one(c, "SELECT * FROM seal_run WHERE id=%s AND attempt_epoch=%s", (run_id, epoch))
        binding = one(c, "SELECT * FROM seal_binding WHERE id=%s", (run["binding_id"],))
    return {
        **run,
        "deadline": run["deadline"].isoformat(),
        "config": binding["config"],
        "params": binding["params"],
        "recipe_version": binding["recipe_version"],
    }


def receipt(run_id):
    with connect() as c:
        run = one(c, "SELECT * FROM seal_run WHERE id=%s", (run_id,))
    return {
        "run_id": run_id,
        "status": run["status"],
        "attempt_epoch": run["attempt_epoch"],
        "errors": run["errors"],
        "report": run["report"],
    }


def execute_run(run_id):
    from .completion import finish_run

    state = receipt(run_id)
    if state["status"] == "finishing":
        return finish_run(run_id, state["attempt_epoch"])
    epoch = begin_attempt(run_id)
    if epoch is None:
        return receipt(run_id)
    context = run_context(run_id, epoch)
    with connect() as c:
        recipe = one(
            c, "SELECT manifest FROM seal_recipe_version WHERE id=%s", (context["recipe_version"],)
        )
    python = recipe["manifest"]["python"]
    # Child stderr is not copied into artifacts: Scrapy errors may contain source data.
    process = subprocess.Popen(
        [python, "-m", "seal.crawl", run_id, str(epoch), str(os.getpid())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    timeout = min(
        context["config"]["budget"]["seconds"] + 15,
        (datetime.fromisoformat(context["deadline"]) - datetime.now(timezone.utc)).total_seconds(),
    )
    start = time.monotonic()
    cancelled = False
    try:
        while process.poll() is None:
            if time.monotonic() - start >= timeout:
                cancelled = True
                break
            with connect() as c:
                source = one(
                    c,
                    "SELECT generation,paused FROM seal_source WHERE id=%s",
                    (context["source_id"],),
                )
            if context["mode"] != "replay" and (
                source["generation"] != context["generation"] or source["paused"]
            ):
                cancelled = True
                break
            time.sleep(0.2)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    state = receipt(run_id)
    if state["status"] in ("complete", "partial", "failed", "superseded"):
        return state
    if state["status"] == "finishing":
        return finish_run(run_id, epoch)
    with connect() as c:
        exhausted = epoch >= context["max_attempts"] or datetime.now(
            timezone.utc
        ) >= datetime.fromisoformat(context["deadline"])
        c.execute(
            "UPDATE seal_run SET status=%s,errors=errors || %s WHERE id=%s AND attempt_epoch=%s AND status='running'",
            (
                "failed" if exhausted else "retryable",
                j(["crawl_cancelled" if cancelled else "child_process_failed"]),
                run_id,
                epoch,
            ),
        )
    return receipt(run_id)
