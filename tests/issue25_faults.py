"""Independent process-boundary acceptance through real SEAL collect commands."""

import argparse
import io
import json
import os
import signal
import subprocess
import sys
import time

from pypdf import PdfReader, PdfWriter

from issue25_e2e import observations
from issue25_engine import install, read_events
from issue25_fixtures import FixtureServer, fixtures, writer_bytes
from issue25_lab import Lab, ROOT, implementation_digest


def live_processes(pids):
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,pgid=,stat="], text=True)
    rows = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 4 and (int(fields[0]) in pids or int(fields[2]) in pids):
            rows.append({"pid": int(fields[0]), "ppid": int(fields[1]), "pgid": int(fields[2]), "state": fields[3]})
    return rows


def versions(lab, source_id):
    with lab.connect() as conn:
        return conn.execute("""SELECT count(*) AS n FROM seal.version v
            JOIN seal.resource r ON r.id=v.resource_id JOIN seal.frontier f ON f.id=r.frontier_id
            WHERE f.source_id=%s""", (source_id,)).fetchone()["n"]


def codes(lab, source_id, receipt):
    rows = observations(lab, source_id)
    with lab.connect() as conn:
        runs = conn.execute("SELECT errors FROM seal.run WHERE source_id=%s", (source_id,)).fetchall()
    return {r["error"] for r in rows if r["error"]} | {e for r in runs for e in r["errors"]} | set((receipt or {}).get("errors", []))


def background(lab, source, env, log, signal_at, shutdown_target=None):
    started = time.monotonic()
    stdout = open(source.with_suffix(".stdout"), "wb")
    stderr = open(source.with_suffix(".stderr"), "wb")
    process = subprocess.Popen([sys.executable, "-m", "seal", "collect", str(source), "--no-progress"],
                               cwd=ROOT, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
    sent = False
    until = time.monotonic() + 20
    try:
        while process.poll() is None and time.monotonic() < until:
            phase, _, threshold = signal_at.partition("*")
            if sum(e["kind"] == phase for e in read_events(log)) >= int(threshold or "1"):
                selected = [e for e in read_events(log) if e["kind"] == phase][-1]
                if shutdown_target:
                    os.kill(selected[shutdown_target], signal.SIGTERM)
                else:
                    process.send_signal(signal.SIGINT)
                sent = True
                break
            time.sleep(0.01)
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
    finally:
        stdout.close()
        stderr.close()
    raw = source.with_suffix(".stdout").read_text()
    receipt = None
    for line in raw.splitlines()[::-1]:
        try:
            receipt = json.loads(line)
            break
        except ValueError:
            pass
    lab.commands.append({"args": ["collect", str(source), "--no-progress"], "exit": process.returncode,
                         "seconds": round(time.monotonic() - started, 3), "signal_sent": sent, "receipt": receipt})
    lab.save()
    return sent, receipt


def assert_cleanup(lab, name, log, *, allow_retained=False):
    events = read_events(log)
    pids = {e["pid"] for e in events if "pid" in e}
    for _ in range(100):
        remaining = live_processes(pids)
        if not remaining:
            break
        time.sleep(0.03)
    lab.check(name + ":processes_reaped", not remaining, remaining)
    lab.check(name + ":credential_isolation", not any(e.get("has_credentials") for e in events))
    if not allow_retained:
        lab.check(name + ":workdir_released", not list((lab.work / "runtime").rglob("seal-pdf-*")))
    (lab.out / (name + "-lifecycle.json")).write_text(json.dumps(events, indent=2))
    return events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="issue25-faults-" + time.strftime("%Y%m%d_%H%M%S"))
    args = parser.parse_args()
    lab = Lab(args.name)
    try:
        data = fixtures(lab.work / "inputs")
        one = PdfWriter()
        one.add_page(PdfReader(io.BytesIO(data["scan"])).pages[0])
        short = writer_bytes(one)
        data.update({"scan_short": short, **{"scan_" + str(i): short for i in range(8)}})
        lab.cli("init")
        with FixtureServer(data) as server:
            counter = 100

            def case(name, mode, expected, *, kind="worker", seconds=30, total=10,
                     page_timeout=30000, hook_mode=None, signal_at=None, urls=None,
                     success=False, retained=False, shutdown_target=None, ocr_overrides=None):
                nonlocal counter
                source_id = "dd-" + str(counter).zfill(3)
                counter += 1
                extra, log = install(lab.work / name, mode, kind=kind, hook_mode=hook_mode)
                env = dict(lab.env, **extra)
                source = lab.source(source_id, urls or [server.base + "/scan.pdf"],
                                    seconds=seconds, timeout_ms=page_timeout)
                import yaml
                config = yaml.safe_load(source.read_text())
                config["ocr"]["timeout_seconds"] = total
                config["ocr"]["max_result_bytes"] = 8192
                config["ocr"].update(ocr_overrides or {})
                source.write_text(yaml.safe_dump(config))
                (lab.out / (source_id + ".yaml")).write_bytes(source.read_bytes())
                if signal_at:
                    sent, receipt = background(lab, source, env, log, signal_at, shutdown_target)
                    lab.check(name + ":signal_during_target_phase", sent)
                else:
                    _, receipt = lab.cli("collect", source, "--no-progress", env=env, timeout=60)
                actual = codes(lab, source_id, receipt)
                if expected:
                    lab.check(name + ":stable_error", expected in actual, {"expected": expected, "actual": sorted(actual)})
                lab.check(name + (":success_version" if success else ":no_success_version"),
                          versions(lab, source_id) == (len(urls) if urls else 1) if success else versions(lab, source_id) == 0)
                events = assert_cleanup(lab, name, log, allow_retained=retained)
                return events, source_id

            for mode in ["invalid_json", "no_preflight", "wrong_error_type", "wrong_method_type", "missing_page", "selfconsistent_missing", "wrong_index", "partial_failed"]:
                case(mode, mode, "pdf_output_invalid")
            case("all_empty_output", "empty", "pdf_no_text")
            case("oversized_worker_output", "stdout_flood", "pdf_output_too_large")
            case("nonzero_worker_exit", "exit_nonzero", "pdf_ocr_failed")
            case("recognizer_nonzero", "exit_nonzero", "pdf_ocr_failed", kind="recognizer")
            case("recognizer_stdout_bound", "stdout_flood", "pdf_output_too_large", kind="recognizer")
            case("recognizer_stderr_bound", "stderr_flood", None, kind="recognizer", success=True)
            case("actual_ocr_page_limit", "unused", "pdf_ocr_page_limit_exceeded", kind="actual",
                 ocr_overrides={"max_pages": 2})
            case("actual_ocr_pixel_limit", "unused", "pdf_ocr_pixel_limit_exceeded", kind="actual",
                 ocr_overrides={"max_page_pixels": 10000})
            case("native_ignores_ocr_page_limit", "unused", None, kind="actual", success=True,
                 urls=[server.base + "/native.pdf"], ocr_overrides={"max_pages": 1})
            case("page_timeout_does_not_cut_ocr", "slow", None, kind="recognizer", total=15,
                 page_timeout=100, success=True)
            case("ocr_timeout_group", "hang", "pdf_ocr_timeout", kind="recognizer", total=3)
            case("budget_timeout_group", "hang", "crawl_time_budget_exceeded", kind="recognizer", seconds=3, total=20)
            case("direct_child_exits_descendant_remains", "exit_with_descendant", None, success=True)
            case("running_cancel", "hang", None, kind="recognizer", signal_at="recognize_start")
            case("result_cancel_race", "result_race", None, signal_at="result_ready")
            case("launch_cancel_race", "hang", None, hook_mode="pipe_delay", signal_at="pipe_ready")
            case("normal_crawl_shutdown", "hang", None, signal_at="worker_start", shutdown_target="ppid")
            case("cancel_during_archive", "slow", None, hook_mode="archive_block", signal_at="archive_block", shutdown_target="pid")

            urls = [server.base + "/scan_" + str(i) + ".pdf" for i in range(8)]
            events, _ = case("bounded_concurrency", "slow", None, urls=urls, success=True)
            changes = []
            for e in events:
                if e["kind"] == "worker_start":
                    changes.append((e["at"], 1))
                elif e["kind"] == "worker_output":
                    changes.append((e["at"], -1))
            active = peak = 0
            for _, change in sorted(changes):
                active += change
                peak = max(peak, active)
            lab.check("bounded_concurrency:at_most_four", 1 <= peak <= 4, {"peak": peak})
            first_output = min((e["at"] for e in events if e["kind"] == "worker_output"), default=0)
            early_gets = [e for e in server.events if e["at"] < first_output and "/scan_" in e["path"]]
            lab.check("bounded_concurrency:event_loop_progress_during_ocr", len(early_gets) > 1, {"concurrent_downloads_before_output": len(early_gets)})

            events, _ = case("waiting_budget", "hang", "crawl_time_budget_exceeded", seconds=3,
                             total=20, urls=urls)
            lab.check("waiting_budget:no_fifth_start", len([e for e in events if e["kind"] == "worker_start"]) <= 4)
            events, _ = case("waiting_cancel", "hang", None, urls=urls, signal_at="worker_start*4")
            lab.check("waiting_cancel:no_fifth_start", len([e for e in events if e["kind"] == "worker_start"]) <= 4)
            events, _ = case("cleanup_failure", "slow", "pdf_cleanup_failed", hook_mode="cleanup_failure",
                             urls=urls, retained=True)
            refusal = min((e["at"] for e in events if e["kind"] == "cleanup_refused"), default=0)
            lab.check("cleanup_failure:admission_closed", bool(refusal) and not any(e["kind"] == "spawn" and e["at"] > refusal for e in events))

        lab.check("implementation_frozen_during_acceptance", lab.environment["implementation_sha256"] == implementation_digest())
        passed = sum(c["status"] == "PASS" for c in lab.checks)
        failed = sum(c["status"] == "FAIL" for c in lab.checks)
        print(json.dumps({"passed": passed, "failed": failed, "report": str(lab.out / "report.json")}))
        return bool(failed)
    finally:
        lab.close()


if __name__ == "__main__":
    raise SystemExit(main())
