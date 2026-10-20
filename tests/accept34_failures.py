#!/usr/bin/env python3
"""Fresh issue #34 coverage/failure and fixed-Snapshot acceptance via real CLI."""

import hashlib
import json
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
import yaml


def run_case(name, configure=None, expected_terms=()):
    p = t.config(name)
    if configure:
        data = yaml.safe_load(Path(p).read_text())
        configure(data)
        Path(p).write_text(yaml.safe_dump(data, allow_unicode=True))
        t.save(name + "-config.json", data)
    before = t.query("SELECT * FROM seal.version ORDER BY id")
    receipt, rc = t.cmd("collect", p, "--no-progress")
    run = t.query("SELECT * FROM seal.run WHERE id=%s", (receipt.get("run_id"),))
    t.save(name + "-run.json", run)
    report = receipt.get("report", {})
    t.check(
        name + " failure explicit",
        bool(report.get("errors") or report.get("failures") or report.get("truncated")),
        report,
    )
    if expected_terms:
        text = json.dumps(
            {k: report.get(k) for k in ("failures", "errors", "coverage", "truncated")}
        ).lower()
        t.check(name + " classification", any(x in text for x in expected_terms), report)
    t.check(
        name + " preserves successful versions",
        t.query("SELECT * FROM seal.version ORDER BY id") == before,
    )
    return receipt


def cancel_case():
    t.MODE["gate"] = "/api/v1/documents"
    t.MODE["gate_entered"] = False
    out = (t.ART / "cancel.stdout").open("w")
    err = (t.ART / "cancel.stderr").open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "seal", "collect", t.config("cancel"), "--no-progress"],
        cwd=t.ROOT,
        env=t.ENV,
        stdout=out,
        stderr=err,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not t.MODE.get("gate_entered") and time.monotonic() < deadline:
            time.sleep(0.03)
        before = t.query("SELECT * FROM seal.version ORDER BY id")
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(10)
        t.MODE["gate"] = None
        out.close()
        err.close()
        t.check("cancel exits130", proc.returncode == 130, proc.returncode)
        t.check(
            "cancel commits no success after signal",
            t.query("SELECT * FROM seal.version ORDER BY id") == before,
        )
        runs = t.query("SELECT * FROM seal.run ORDER BY created_at")
        t.save("cancel-runs.json", runs)
        t.check(
            "cancel classification preserved",
            runs[-1].get("report", {}).get("finish_reason")
            in ("cancelled", "canceled", "interrupted")
            and runs[-1].get("report", {}).get("failures", {}).get("network_failed", 0) == 0,
            runs[-1].get("report"),
        )
    finally:
        t.MODE["gate"] = None


def main():
    t.dbsetup()
    server = ThreadingHTTPServer(("127.0.0.1", 0), t.Origin)
    t.BASE = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        t.MODE["inline"] = False
        t.cmd("init", expected=[0])
        t.cmd("collect", t.config("baseline"), "--no-progress", expected=[0])
        sid = t.snapshot("A")
        frozen = t.api(sid, "A-before-failures")
        snap_before = t.query("SELECT * FROM seal.snapshot WHERE id=%s", (sid,))
        coverage_before = t.query("SELECT * FROM seal.snapshot_source WHERE snapshot_id=%s", (sid,))
        t.save("A-coverage.json", coverage_before)
        run_case(
            "request-budget",
            lambda x: x["budget"].update(requests=2),
            ("request_budget_exceeded", "budget_truncated"),
        )
        run_case(
            "document-budget",
            lambda x: x["budget"].update(documents=2),
            ("document_budget_exceeded", "budget_truncated"),
        )
        t.MODE["fail"] = "/api/v1/documents"
        run_case("network-failure", expected_terms=("network", "request_failed", "http_503"))
        t.MODE["fail"] = None
        t.MODE["fail"] = "2026-10001.pdf"
        run_case(
            "missing-required-attachment", expected_terms=("required", "attachment", "input_failed")
        )
        t.MODE["fail"] = None
        run_case(
            "quality-failure",
            lambda x: x.update(parser={"quality_accept_routes": ["accept_ocr"]}),
            ("quality_check_failed", "parse_failed", "quality"),
        )
        t.MODE["invalid_xml"] = True
        run_case("parse-failure", expected_terms=("parse", "xml", "document_failed"))
        t.MODE["invalid_xml"] = False
        t.MODE["pages"] = 2
        run_case(
            "incomplete-enumeration",
            lambda x: x["adapter_options"].update(max_pages=1),
            ("enumeration", "incomplete", "page_limit"),
        )
        t.MODE["pages"] = 1
        cancel_case()
        t.check(
            "Snapshot A object bytes fixed",
            t.query("SELECT * FROM seal.snapshot WHERE id=%s", (sid,)) == snap_before,
        )
        t.check(
            "Snapshot A frozen coverage fixed",
            t.query("SELECT * FROM seal.snapshot_source WHERE snapshot_id=%s", (sid,))
            == coverage_before,
        )
        after_failures = t.api(sid, "A-after-failures")
        t.check(
            "Snapshot A API records and file bytes fixed after failures/cancellation",
            after_failures == frozen,
        )
        row = t.query("SELECT original_hash, markdown_hash FROM seal.delivery_row LIMIT 1")[0]
        sha = row["original_hash"] or row["markdown_hash"]
        s3 = t.s3client()
        original = s3.get_object(Bucket=t.ENV["SEAL_S3_BUCKET"], Key=sha)["Body"].read()
        try:
            s3.put_object(Bucket=t.ENV["SEAL_S3_BUCKET"], Key=sha, Body=b"corrupt fixture object")
            with t.api_session("corrupt-CAS") as get:
                status, body, headers = get("/files/" + sha)
            t.check(
                "files API rejects corrupt CAS object",
                500 <= status < 600 and body != b"corrupt fixture object",
                {"status": status, "body": body.decode(errors="replace")},
            )
            t.check(
                "corrupt CAS response remains explicit",
                "corrupt" in body.decode(errors="replace").lower(),
            )
        finally:
            s3.put_object(Bucket=t.ENV["SEAL_S3_BUCKET"], Key=sha, Body=original)
        restored = t.api(sid, "A-after-CAS-repair")
        t.check("Snapshot A API bytes fixed after isolated CAS repair", restored == frozen)
    except Exception as exc:
        t.save("exception.json", {"exception": str(exc), "traceback": traceback.format_exc()})
        t.check("failure harness completes", False, str(exc))
        traceback.print_exc()
    finally:
        t.MODE["gate"] = None
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
