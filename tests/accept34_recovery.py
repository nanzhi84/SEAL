#!/usr/bin/env python3
"""Real CLI crash/takeover acceptance. PostgreSQL advisory locks freeze business boundaries."""

import argparse
import contextlib
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path

import accept34_e2e as t
import psycopg

FAMILIES = {}


def signal_tree(proc, sig):
    pairs = [
        tuple(map(int, line.split()))
        for line in subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True).splitlines()
    ]
    family = {proc.pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in pairs:
            if ppid in family and pid not in family:
                family.add(pid)
                changed = True
    family |= FAMILIES.get(proc.pid, set())
    FAMILIES[proc.pid] = family
    for pid in sorted(family, reverse=True):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)


def poll(fn, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.04)
    raise TimeoutError("acceptance boundary not reached")


def capture(label):
    state = {
        table: t.query("SELECT * FROM seal." + table)
        for table in [
            "run",
            "frontier",
            "task",
            "acquisition",
            "observation",
            "version",
            "delivery_conflict",
        ]
    }
    t.save(label + ".json", state)
    return state


def install_fault(point, key):
    clauses = {
        "archived-before-targets": ("BEFORE INSERT ON seal.task", "NEW.kind='target'"),
        "partial-targets": (
            "BEFORE INSERT ON seal.observation",
            "(SELECT count(*) FROM seal.observation WHERE run_id=NEW.run_id)=2",
        ),
        "delivered-before-checkpoint": (
            "BEFORE UPDATE OF checkpoint ON seal.frontier",
            "EXISTS(SELECT 1 FROM seal.observation WHERE run_id=NEW.run_id)",
        ),
    }
    event, condition = clauses[point]
    statement = f"""CREATE OR REPLACE FUNCTION seal.accept34_pause() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN IF {condition} THEN PERFORM pg_advisory_xact_lock({key}); END IF; RETURN NEW; END $$;
CREATE TRIGGER accept34_pause {event} FOR EACH ROW EXECUTE FUNCTION seal.accept34_pause();"""
    t.query(statement)
    t.save(
        point + "-fault-injection.json",
        {
            "sql": statement,
            "method": "advisory lock held by harness, SIGKILL old CLI after durable boundary is visible",
        },
    )
    return event.split(" ON ")[1]


def crash(point, index):
    key = 34000 + index
    owner = psycopg.connect(t.ENV["SEAL_DATABASE_URL"], autocommit=True)
    owner.execute("SELECT pg_advisory_lock(%s)", (key,))
    table = install_fault(point, key)
    stdout = (t.ART / (point + "-killed.stdout")).open("w")
    stderr = (t.ART / (point + "-killed.stderr")).open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "seal", "collect", t.config(point), "--no-progress"],
        cwd=t.ROOT,
        env=t.ENV,
        text=True,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    t.PROCESSES.append(proc)
    try:
        poll(
            lambda: t.query(
                "SELECT pid FROM pg_locks WHERE locktype=%s AND NOT granted AND objid=%s",
                ("advisory", key),
            )
        )
        network_before = len(t.REQUESTS)
        before = capture(point + "-before-kill")
        run = sorted(before["run"], key=lambda r: r["created_at"])[-1]
        rid = run["id"]
        obs = [o for o in before["observation"] if o["run_id"] == rid]
        acs = [a for a in before["acquisition"] if a["run_id"] == rid]
        expected = {
            "archived-before-targets": 0,
            "partial-targets": 2,
            "delivered-before-checkpoint": 5,
        }[point]
        t.check(
            point + " durable delivery count",
            len(obs) == expected,
            {"count": len(obs), "archived": len([a for a in acs if a["state"] == "archived"])},
        )
        t.check(point + " has archived original", any(a["state"] == "archived" for a in acs))
        signal_tree(proc, signal.SIGKILL)
        proc.wait(10)
        stdout.close()
        stderr.close()
        owner.execute("SELECT pg_advisory_unlock(%s)", (key,))
        owner.close()
        t.query("DROP TRIGGER accept34_pause ON " + table)
        t.query("DROP FUNCTION seal.accept34_pause()")
        # Real recovery timeout is 30 seconds. Keep the original attempt state and wait naturally.
        time.sleep(32)
        try:
            t.cmd("worker", "--once", timeout=35)
        except subprocess.TimeoutExpired:
            t.check(point + " worker exits within bound", False)
        t.check(
            point + " archived replay makes no source request",
            len(t.REQUESTS) == network_before,
            {"before": network_before, "after": len(t.REQUESTS)},
        )
        after = capture(point + "-after-worker")
        run = next(r for r in after["run"] if r["id"] == rid)
        obs = [o for o in after["observation"] if o["run_id"] == rid]
        t.check(
            point + " takeover complete",
            run["status"] == "complete",
            {"status": run["status"], "errors": run["errors"], "epoch": run["attempt_epoch"]},
        )
        t.check(
            point + " five unique deliveries",
            len(obs) == 5 and len(set(o["resource_id"] for o in obs)) == 5,
            {"observations": len(obs)},
        )
        versions = after["version"]
        t.check(point + " no duplicate versions", len(versions) == 5, {"versions": len(versions)})
        t.check(point + " new fenced attempt", run["attempt_epoch"] > 1)
    finally:
        with contextlib.suppress(Exception):
            owner.execute("SELECT pg_advisory_unlock(%s)", (key,))
            owner.close()
        with contextlib.suppress(Exception):
            t.query("DROP TRIGGER IF EXISTS accept34_pause ON " + table)
            t.query("DROP FUNCTION IF EXISTS seal.accept34_pause()")
        if proc.poll() is None:
            signal_tree(proc, signal.SIGKILL)
            proc.wait(10)


def stale_attempt():
    t.MODE.update(gate="/api/v1/documents", gate_entered=False, gate_once=True, gate_used=False)
    out = (t.ART / "stale-attempt.stdout").open("w")
    err = (t.ART / "stale-attempt.stderr").open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "seal", "collect", t.config("stale-attempt"), "--no-progress"],
        cwd=t.ROOT,
        env=t.ENV,
        stdout=out,
        stderr=err,
        start_new_session=True,
    )
    try:
        poll(lambda: t.MODE.get("gate_entered"))
        signal_tree(proc, signal.SIGSTOP)
        before = capture("stale-attempt-before-takeover")
        rid = sorted(before["run"], key=lambda r: r["created_at"])[-1]["id"]
        terminated = t.query(
            "SELECT pid,pg_terminate_backend(pid) AS terminated FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
        )
        t.save(
            "stale-attempt-connection-loss.json",
            {
                "method": "SIGSTOP attempt plus isolated PostgreSQL connection loss releases its session lease; old in-memory attempt then resumes after worker takeover",
                "backends": terminated,
            },
        )
        time.sleep(32)
        t.cmd("worker", "--once", timeout=35)
        taken = capture("stale-attempt-taken-over")
        run = next(r for r in taken["run"] if r["id"] == rid)
        t.check(
            "paused old attempt replaced by worker",
            run["status"] == "complete" and run["attempt_epoch"] == 2,
            {"status": run["status"], "epoch": run["attempt_epoch"]},
        )
        old_acq = [
            a for a in taken["acquisition"] if a["run_id"] == rid and a["attempt_epoch"] == 1
        ]
        t.check(
            "old running acquisition abandoned",
            old_acq and all(a["state"] == "abandoned" for a in old_acq),
        )
        t.MODE["gate"] = None
        signal_tree(proc, signal.SIGCONT)
        proc.wait(20)
        out.close()
        err.close()
        after = capture("stale-attempt-after-resume")
        for table in ("run", "frontier", "task", "acquisition", "observation", "version"):
            t.check(
                "stale attempt cannot change " + table,
                sorted(taken[table], key=lambda r: str(r.get("id")))
                == sorted(after[table], key=lambda r: str(r.get("id"))),
            )
        t.check(
            "stale attempt cannot deliver observations",
            all(o["attempt_epoch"] == 2 for o in after["observation"] if o["run_id"] == rid),
        )
    finally:
        t.MODE.update(gate=None, gate_once=False)
        if proc.poll() is None:
            signal_tree(proc, signal.SIGCONT)
            signal_tree(proc, signal.SIGKILL)
            proc.wait(10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["all", "stale"], default="all")
    args = parser.parse_args()
    t.MODE["inline"] = True
    t.dbsetup()
    server = ThreadingHTTPServer(("127.0.0.1", 0), t.Origin)
    t.BASE = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        t.cmd("init", expected=[0])
        for index, point in enumerate(
            ["archived-before-targets", "partial-targets", "delivered-before-checkpoint"]
            if args.phase == "all"
            else [],
            1,
        ):
            crash(point, index)
        stale_attempt()
        snap = t.snapshot("recovered")
        t.api(snap, "recovered")
    except Exception as exc:
        t.save("exception.json", {"exception": str(exc), "traceback": traceback.format_exc()})
        t.check("recovery harness completes", False, str(exc))
        traceback.print_exc()
    finally:
        t.save(
            "provenance.json",
            {
                "head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=t.ROOT, text=True
                ).strip(),
                "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "support_sha256": hashlib.sha256(
                    (t.ROOT / "tests/accept34_e2e.py").read_bytes()
                ).hexdigest(),
                "source_hashes": t.SOURCE_AT_START,
                "tests_start_sha256": t.TESTS_AT_START,
                "source_end_hashes": {
                    str(p.relative_to(t.ROOT / "src")): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((t.ROOT / "src").rglob("*"))
                    if p.is_file() and p.suffix in (".py", ".sql")
                },
                "artifact": str(t.ART),
            },
        )
        t.save("assertions.json", t.ASSERTIONS)
        print("ARTIFACT", t.ART, flush=True)


if __name__ == "__main__":
    main()
    sys.exit(any(not a["passed"] for a in t.ASSERTIONS))
