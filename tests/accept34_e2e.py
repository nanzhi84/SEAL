#!/usr/bin/env python3
"""Fresh issue #34 business acceptance: real CLI, isolated DB/S3, local origin."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import accept34_api as api_support
import accept34_fixture as source_fixture
import boto3
import psycopg
from accept34_fixture import pdf
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(3)
WORK = ROOT / "work" / "accept34" / STAMP
ART = ROOT / "artifacts" / "accept34" / STAMP
for p in (WORK, ART):
    p.mkdir(parents=True, exist_ok=True)
ENV = dict(
    os.environ,
    PYTHONPATH=str(ROOT / "src"),
    SEAL_ALLOW_LOOPBACK="1",
    SEAL_WORK_DIR=str(WORK / "runtime"),
)
COMMANDS = []
ASSERTIONS = []
REQUESTS = []
PROCESSES = []
SOURCE_AT_START = {
    str(p.relative_to(ROOT / "src")): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted((ROOT / "src").rglob("*"))
    if p.is_file() and p.suffix in (".py", ".sql")
}
TESTS_AT_START = {
    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted((ROOT / "tests").glob("accept34*"))
    if p.is_file()
}
BASE = ""
MODE = {"revision": 0, "fail": None, "gate": None, "inline": False}
SOURCE = "dd-499"


def save(name, data):
    (ART / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")


def check(name, ok, detail=None):
    row = {"assertion": name, "passed": bool(ok), "detail": detail}
    ASSERTIONS.append(row)
    save("assertions.json", ASSERTIONS)
    print(
        json.dumps(
            {"assertion": name, "passed": bool(ok), "detail": str(detail)[:600]},
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )
    return bool(ok)


def cmd(*args, timeout=120, expected=None, env=None):
    started = time.monotonic()
    p = subprocess.run(
        [sys.executable, "-m", "seal", *args],
        cwd=ROOT,
        env=env or ENV,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    row = {
        "command": ["python", "-m", "seal", *args],
        "returncode": p.returncode,
        "seconds": round(time.monotonic() - started, 3),
        "stdout": p.stdout,
        "stderr": p.stderr,
    }
    COMMANDS.append(row)
    save("commands.json", COMMANDS)
    try:
        result = json.loads(p.stdout)
    except ValueError:
        result = {"raw": p.stdout}
    print("CLI", args, "=>", p.returncode, str(result)[:500], flush=True)
    if expected is not None:
        check("CLI exit " + " ".join(args[:2]), p.returncode in expected, row)
    return result, p.returncode


def query(statement, args=()):
    with psycopg.connect(ENV["SEAL_DATABASE_URL"]) as c:
        with c.cursor() as cur:
            cur.execute(statement, args)
            return (
                [
                    dict(zip([d.name for d in cur.description], r, strict=True))
                    for r in cur.fetchall()
                ]
                if cur.description
                else []
            )


def dbsetup():
    cfg = conninfo_to_dict(os.environ["SEAL_DATABASE_URL"])
    db = "accept34_" + STAMP.lower()
    admin = make_conninfo(**dict(cfg, dbname="postgres"))
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
    ENV["SEAL_DATABASE_URL"] = make_conninfo(**dict(cfg, dbname=db))
    ENV["SEAL_S3_BUCKET"] = "accept34-" + STAMP.lower().replace("_", "-")
    s3 = boto3.client(
        "s3",
        endpoint_url=ENV["SEAL_S3_ENDPOINT"],
        aws_access_key_id=ENV["SEAL_S3_ACCESS_KEY"],
        aws_secret_access_key=ENV["SEAL_S3_SECRET_KEY"],
        region_name=ENV.get("SEAL_S3_REGION", "us-east-1"),
    )
    s3.create_bucket(Bucket=ENV["SEAL_S3_BUCKET"])
    (WORK / "environment.json").write_text(
        json.dumps({k: ENV[k] for k in ENV if k.startswith("SEAL_")})
    )
    (WORK / "environment.json").chmod(0o600)
    save(
        "environment.json",
        {
            "database": db,
            "bucket": ENV["SEAL_S3_BUCKET"],
            "python": sys.executable,
            "PYTHONPATH": ENV["PYTHONPATH"],
            "source": "localhost controlled fixture",
            "run": STAMP,
            "source_start_sha256": SOURCE_AT_START,
            "tests_start_sha256": TESTS_AT_START,
        },
    )
    return s3


def document(n):
    num = f"2026-{10000 + n}"
    rev = MODE["revision"] if n == 1 else 0
    item = {
        "document_number": num,
        "title": f"Acceptance legal rule {n}",
        "type": "Rule",
        "publication_date": f"2026-10-{10 + n:02d}",
        "html_url": None if n == 5 else f"{BASE}/documents/{num}",
        "full_text_xml_url": f"{BASE}/documents/full_text/xml/{num}.xml",
        "pdf_url": f"{BASE}/content/pkg/{num}.pdf",
        "agencies": [{"name": "Acceptance Agency"}],
        "abstract": f"Public legal rule {n} revision {rev}.",
        "action": "Final rule",
        "dates": "Effective October 20, 2026.",
    }
    if MODE["inline"]:
        item.pop("full_text_xml_url")
        item.pop("pdf_url")
        item["body"] = (
            f"This regulation establishes public procedures and duties for document {n}. All designated authorities shall observe these procedures. Revision {rev}."
        )
    return item


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        REQUESTS.append({"path": self.path, "time": time.time(), "revision": MODE["revision"]})
        save("requests.json", REQUESTS)
        gate = MODE.get("gate")
        if gate and gate in path.path and (not MODE.get("gate_once") or not MODE.get("gate_used")):
            MODE["gate_used"] = True
            MODE["gate_entered"] = True
            while MODE.get("gate") == gate:
                time.sleep(0.02)
        status = 200
        kind = "application/json"
        body = b""
        if MODE.get("fail") and MODE["fail"] in path.path:
            status = 503
            body = b"failure fixture"
        elif path.path == "/robots.txt":
            kind = "text/plain"
            body = b"User-agent: *\nAllow: /\n"
        elif path.path.endswith(".xml"):
            n = int(path.path.rsplit("/", 1)[-1].split(".")[0].split("-")[-1]) - 10000
            kind = "application/xml"
            body = f"<DOC><HD>Acceptance legal rule {n}</HD><P>This regulation establishes public procedures and duties for document {n}. All designated authorities shall observe these procedures.</P><P>Revision {MODE['revision'] if n == 1 else 0}.</P></DOC>".encode()
            if MODE.get("invalid_xml"):
                body = b"<DOC><P>unclosed invalid XML"
        elif path.path.endswith(".pdf"):
            kind = "application/pdf"
            body = pdf(int(path.path.rsplit("/", 1)[-1].split(".")[0].split("-")[-1]) - 10000)
        elif "/documents" in path.path:
            nums = [
                int(x[-5:]) - 10000
                for x in path.path.split("/")[-1].replace(".json", "").split(",")
                if x.startswith("2026-")
            ]
            if not nums:
                nums = list(range(1, 6))
            docs = [] if MODE.get("empty") else [document(n) for n in nums]
            if MODE.get("duplicate"):
                docs.append(
                    dict(
                        docs[0],
                        body=docs[0].get("body", "")
                        + (
                            " Additional amended mandatory legal duties."
                            if MODE["duplicate"] == "changed"
                            else ""
                        ),
                    )
                )
            # Federal Register singleton endpoint returns a bare record.
            data = {
                "count": len(docs),
                "total_pages": MODE.get("pages", 1),
                "next_page_url": BASE + "/api/v1/documents.json?page=2"
                if MODE.get("pages", 1) > 1
                else None,
                "results": docs,
            }
            if len(nums) == 1:
                data = docs[0] if docs else {"error": "document_not_found"}
            if len(nums) == 1 and not docs:
                status = 404
            if MODE["revision"] % 2 and "results" in data:
                data["results"] = list(reversed(data["results"]))
            body = json.dumps(data).encode()
        else:
            status = 404
            body = b"not found"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)


def config(*args, **kwargs):
    return source_fixture.config(sys.modules[__name__], *args, **kwargs)


def snapshot(label):
    receipt, rc = cmd("snapshot", "create", expected=[0])
    save(label + "-snapshot-receipt.json", receipt)
    rows = query("SELECT id FROM seal.snapshot ORDER BY created_at DESC LIMIT 1")
    sid = str(rows[0]["id"]) if rows else None
    if sid:
        cmd("snapshot", "publish", sid, expected=[0])
    return sid


def s3client():
    return api_support.s3client(sys.modules[__name__])


def api_session(label):
    return api_support.api_session(sys.modules[__name__], label)


def api(sid, label="api", after_first_page=None, check_files=True):
    return api_support.api(sys.modules[__name__], sid, label, after_first_page, check_files)


def main():
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["baseline", "all"], default="baseline")
    parser.add_argument("--fixture", choices=["inline", "metadata"], default="inline")
    args = parser.parse_args()
    MODE["inline"] = args.fixture == "inline"
    dbsetup()
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    BASE = f"http://127.0.0.1:{origin.server_port}"
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    try:
        cmd("init", expected=[0])
        save(
            "schema.json",
            query(
                "SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='seal' ORDER BY table_name,ordinal_position"
            ),
        )
        source = config()
        initial, _ = cmd("collect", source, "--no-progress", expected=[0])
        resources = query("SELECT * FROM seal.resource")
        save("initial-resources.json", resources)
        save("initial-runs.json", query("SELECT * FROM seal.run"))
        check(
            "five documents collected",
            len(resources) == 5,
            {"resources": len(resources), "requests": len(REQUESTS)},
        )
        check("nullable public URL collected", any(r.get("public_url") is None for r in resources))
        if len(resources) != 5:
            return
        snap_a = snapshot("A")
        proof_a = api(snap_a, "A")
        snapshot_before = query("SELECT * FROM seal.snapshot WHERE id=%s", (snap_a,))
        coverage_before = query(
            "SELECT * FROM seal.snapshot_source WHERE snapshot_id=%s", (snap_a,)
        )
        old_versions = query("SELECT * FROM seal.version ORDER BY id")
        save("A-versions.json", old_versions)
        query("UPDATE seal.resource SET next_check=now()-interval '1 hour'")
        start = len(REQUESTS)
        recheck, _ = cmd("recheck", config("recheck", requests=3), "--no-progress")
        req = REQUESTS[start:]
        save("recheck-requests.json", req)
        runs = query("SELECT * FROM seal.run ORDER BY created_at")
        save("recheck-runs.json", runs)
        save("recheck-observations.json", query("SELECT * FROM seal.observation ORDER BY id"))
        check(
            "batch fewer real requests than due resources",
            len(req) < 5,
            {"due": 5, "requests": len(req), "paths": [x["path"] for x in req]},
        )
        check(
            "all five resources rechecked"
            if MODE["inline"]
            else "metadata attachments budget truncates explicitly",
            recheck.get("status") == "complete"
            if MODE["inline"]
            else recheck.get("status") == "failed"
            and recheck.get("report", {}).get("truncated") == "request_budget_exceeded",
            recheck,
        )
        check(
            "unchanged batch does not create versions",
            query("SELECT * FROM seal.version ORDER BY id") == old_versions,
        )
        MODE["revision"] = 1
        query("UPDATE seal.resource SET next_check=now()-interval '1 hour'")
        cmd("recheck", config("changed"), "--no-progress")
        save("changed-versions.json", query("SELECT * FROM seal.version ORDER BY id"))
        check(
            "unrelated document reorder keeps sibling versions",
            len(query("SELECT * FROM seal.version")) == 6,
        )
        source_offline = config(
            "offline",
            parser={
                "office_text_policy": "prefer",
                "quality_accept_routes": ["accept", "accept_ocr"],
                "libreoffice_timeout_seconds": 120,
            },
        )
        before_reparse = query("SELECT * FROM seal.version ORDER BY id")
        before_acq = query("SELECT count(*) AS n FROM seal.acquisition")[0]["n"]
        before_req = len(REQUESTS)
        origin.shutdown()
        origin.server_close()
        reparse, rrc = cmd("reparse", source_offline, "--no-progress")
        save("reparse-runs.json", query("SELECT * FROM seal.run ORDER BY created_at"))
        check("offline reparse completes", rrc == 0, reparse)
        check(
            "offline reparse zero acquisition",
            query("SELECT count(*) AS n FROM seal.acquisition")[0]["n"] == before_acq,
        )
        check("offline zero source request", len(REQUESTS) == before_req)
        reparsed_versions = query("SELECT * FROM seal.version ORDER BY id")
        save("reparse-versions.json", reparsed_versions)
        check(
            "new parser creates five recomputed immutable versions",
            len(reparsed_versions) == len(before_reparse) + 5
            and len([v for v in reparsed_versions if v["change_reason"] == "parser_recomputed"])
            == 5,
        )
        check(
            "old versions byte fixed",
            query(
                "SELECT * FROM seal.version WHERE id=ANY(%s) ORDER BY id",
                ([v["id"] for v in old_versions],),
            )
            == old_versions,
        )
        snap_b = snapshot("B")
        api(snap_b, "B")
        check(
            "Snapshot A immutable database record",
            query("SELECT * FROM seal.snapshot WHERE id=%s", (snap_a,)) == snapshot_before,
        )
        check(
            "Snapshot A immutable frozen coverage",
            query("SELECT * FROM seal.snapshot_source WHERE snapshot_id=%s", (snap_a,))
            == coverage_before,
        )
        cmd("snapshot", "publish", snap_a, expected=[0])
        cursor_a = api(
            snap_a,
            "A-cursor-during-B",
            after_first_page=lambda: cmd("snapshot", "publish", snap_b, expected=[0]),
            check_files=False,
        )
        check("A cursor remains fixed during B publication", cursor_a["items"] == proof_a["items"])
        cmd("snapshot", "publish", snap_a, expected=[0])
        restored_a = api(snap_a, "A-after-B")
        check("Snapshot A API rows and bytes immutable after B", restored_a == proof_a)
        cmd("snapshot", "publish", snap_b, expected=[0])
    except Exception as exc:
        save(
            "exception.json",
            {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()},
        )
        check("harness finished", False, {"type": type(exc).__name__, "message": str(exc)})
        traceback.print_exc()
    finally:
        MODE["gate"] = None
        for p in PROCESSES:
            if p.poll() is None:
                p.terminate()
                p.wait(10)
        save("commands.json", COMMANDS)
        save("requests.json", REQUESTS)
        save("assertions.json", ASSERTIONS)
        save(
            "provenance.json",
            {
                "head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "boundaries_sha256": hashlib.sha256(
                    (ROOT / "tests/accept34_failure_boundaries.json").read_bytes()
                ).hexdigest(),
                "source_hashes": SOURCE_AT_START,
                "source_end_hashes": {
                    str(p.relative_to(ROOT / "src")): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((ROOT / "src").rglob("*"))
                    if p.is_file() and p.suffix in (".py", ".sql")
                },
                "artifact": str(ART),
                "command": "source /Users/yoryon/SEAL/work/dev/env.sh; PYTHONPATH="
                + str(ROOT / "src")
                + " "
                + str(ROOT / ".venv/bin/python")
                + " "
                + str(Path(__file__))
                + " --stage "
                + args.stage
                + " --fixture "
                + args.fixture,
            },
        )
        print("ARTIFACT", ART, flush=True)


if __name__ == "__main__":
    main()
    sys.exit(any(not a["passed"] for a in ASSERTIONS))
