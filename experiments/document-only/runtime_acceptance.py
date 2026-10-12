"""Actual terminal, queue, cancellation and ownership paths with bounded waiting."""

import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time


def until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("bounded_wait_expired")


def terminal(h, command, *, no_progress=False, interrupt=False, width=100):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
    args = [sys.executable, "-m", "seal", *map(str, command)]
    if no_progress:
        args.append("--no-progress")
    process = subprocess.Popen(
        args,
        env=dict(h.env, TERM="xterm-256color"),
        stdout=subprocess.PIPE,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    wire, samples = bytearray(), []
    started = time.monotonic()
    interrupted = False
    try:
        while process.poll() is None:
            if time.monotonic() - started > 100:
                raise AssertionError("terminal_timeout")
            if select.select([master], [], [], 0.1)[0]:
                try:
                    wire.extend(os.read(master, 65536))
                except OSError:
                    pass
            rows = h.sql(
                "SELECT * FROM seal.run WHERE source_id=%s ORDER BY created_at DESC LIMIT 1",
                (command[1].stem,),
            )
            if rows:
                run = rows[0]
                count = h.sql(
                    "SELECT count(DISTINCT url) AS n FROM seal.discovery WHERE run_id=%s AND role NOT IN ('robots','sitemap')",
                    (run["id"],),
                )[0]["n"]
                samples.append({"status": run["status"], "discovered": count})
                if interrupt and run["status"] == "running" and not interrupted:
                    process.send_signal(signal.SIGINT)
                    interrupted = True
            if len(samples) == 3:
                fcntl.ioctl(
                    master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, max(24, width // 2), 0, 0)
                )
                process.send_signal(signal.SIGWINCH)
        while select.select([master], [], [], 0)[0]:
            try:
                data = os.read(master, 65536)
                if not data:
                    break
                wire.extend(data)
            except OSError:
                break
        result = json.loads(process.stdout.read())
        h.check("tty_exit", process.returncode, 130 if interrupt else 0)
        h.check("tty_stdout_json", isinstance(result, dict))
        text = wire.decode(errors="replace")
        h.capture(
            "terminal-" + command[1].stem + ("-off" if no_progress else ""),
            {
                "command": args,
                "stderr": text,
                "result": result,
                "samples": samples,
                "exit": process.returncode,
            },
        )
        if no_progress:
            h.check("tty_no_progress_no_ansi", "\x1b" not in text)
        else:
            h.check("tty_live_rendered", "\x1b" in text and "SEAL" in text)
            if not interrupt:
                h.check("tty_final_complete", "complete" in text)
        return result, samples
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        process.stdout.close()
        os.close(master)


def exercise(h):
    dynamic = h.config("terminal", ["/documents/root", "/documents/image.png", "/documents/slow"])
    state, samples = terminal(h, ["collect", dynamic])
    h.check(
        "dynamic_discovery_denominator_grows",
        max(s["discovered"] for s in samples) > min(s["discovered"] for s in samples),
    )
    counts = state["summary"]["counts"]
    h.check(
        "counter_documents_sql",
        counts["documents"],
        h.sql(
            "SELECT count(DISTINCT document_id) AS n FROM seal.document_emission WHERE run_id=%s",
            (state["run_id"],),
        )[0]["n"],
    )
    h.check(
        "counter_fetched_sql",
        counts["fetched"],
        h.sql(
            "SELECT count(*) AS n FROM seal.fetch_observation WHERE run_id=%s AND is_content",
            (state["run_id"],),
        )[0]["n"],
    )
    h.check("counter_final_pending_active", (counts["pending"], counts["active"]), (0, 0))
    terminal(h, ["recheck", dynamic], width=90)
    terminal(h, ["collect", dynamic], no_progress=True, width=32)
    cancelled = h.config("cancelled", ["/documents/slow"])
    result, _ = terminal(h, ["collect", cancelled], interrupt=True)
    h.check("ctrl_c_receipt", result["error"], "interrupted")
    h.cli("worker", "--once")
    recovered = h.sql("SELECT status,attempt_epoch FROM seal.run WHERE id=%s", (result["run_id"],))[
        0
    ]
    h.check("ctrl_c_recovered", recovered["status"], "complete")
    h.check("ctrl_c_new_attempt", recovered["attempt_epoch"], 2)
    crash = h.config("crash", ["/documents/slow"])
    process = h.spawn("collect", crash)
    old = until(
        lambda: next(
            iter(h.sql("SELECT * FROM seal.run WHERE source_id='crash' AND status='running'")), None
        )
    )
    process.kill()
    process.wait(timeout=10)
    h.cli("worker", "--once")
    h.check(
        "foreground_crash_recovered",
        h.sql("SELECT status,attempt_epoch FROM seal.run WHERE id=%s", (old["id"],))[0],
        {"status": "complete", "attempt_epoch": 2},
    )
    queued_crash = h.config("queued-crash", ["/documents/slow"])
    queued = h.cli("collect", queued_crash, "--enqueue")
    worker = h.spawn("worker")
    until(
        lambda: h.sql(
            "SELECT id FROM seal.run WHERE id=%s AND status='running'", (queued["run_id"],)
        )
    )
    worker.kill()
    worker.wait(timeout=10)
    # Wait for the real library heartbeat threshold; never manufacture a stale job.
    until(
        lambda: h.sql(
            """SELECT j.id FROM public.procrastinate_jobs j
        JOIN public.procrastinate_workers w ON w.id=j.worker_id
        WHERE j.id=(SELECT job_id FROM seal.run WHERE id=%s)
          AND w.last_heartbeat<now()-interval '31 seconds'""",
            (queued["run_id"],),
        ),
        timeout=45,
    )
    h.cli("worker", "--once")
    h.check(
        "worker_crash_recovered",
        h.sql("SELECT status,attempt_epoch FROM seal.run WHERE id=%s", (queued["run_id"],))[0],
        {"status": "complete", "attempt_epoch": 2},
    )
    # A queued old configuration must fail, while its immutable Binding stays unchanged.
    frozen = h.config("fenced", ["/documents/plain.txt"])
    older = h.cli("collect", frozen, "--enqueue")
    before = h.sql("SELECT config,params FROM seal.binding WHERE id=%s", (older["binding_id"],))[0]
    data = json.loads(frozen.read_text())
    data["recipe"]["params"] = {"options": {"format": "txt"}}
    frozen.write_text(json.dumps(data))
    newer = h.cli("collect", frozen, "--enqueue")
    h.check("config_new_binding", older["binding_id"] != newer["binding_id"])
    h.check(
        "binding_immutable",
        h.sql("SELECT config,params FROM seal.binding WHERE id=%s", (older["binding_id"],))[0],
        before,
    )
    h.cli("worker", "--once")
    old_state = h.sql("SELECT status,errors FROM seal.run WHERE id=%s", (older["run_id"],))[0]
    h.check(
        "generation_fencing",
        old_state,
        {"status": "failed", "errors": ["source_generation_changed"]},
    )
    h.check(
        "new_config_complete",
        h.sql("SELECT status FROM seal.run WHERE id=%s", (newer["run_id"],))[0]["status"],
        "complete",
    )
    expired = h.cli("collect", frozen, "--enqueue")
    h.sql(
        "UPDATE seal.run SET deadline=now()-interval '1 second' WHERE id=%s", (expired["run_id"],)
    )
    h.cli("worker", "--once")
    h.check(
        "deadline_terminal",
        h.sql("SELECT status,errors FROM seal.run WHERE id=%s", (expired["run_id"],))[0],
        {"status": "failed", "errors": ["deadline_exceeded"]},
    )
    h.check(
        "lineage_never_cross_attempt",
        h.sql("""SELECT count(*) AS n FROM seal.document_emission e JOIN seal.fetch_observation o ON o.id=e.observation_id
        WHERE e.run_id<>o.run_id OR e.attempt_epoch<>o.attempt_epoch""")[0]["n"],
        0,
    )
    h.capture(
        "runtime-faults", {"cancelled": recovered, "crash": old["id"], "old_config": old_state}
    )
