#!/usr/bin/env python3
"""Independent real CLI PDF fixture, declared before datetime serialization repair.

Failure modes and boundaries:
- A valid text PDF with CreationDate/ModDate crashes diagnostic serialization.
- HTTP input is fetched but its original bytes or SDK text disappear after exit.
- Native metadata datetimes are not representable in the persisted JSON envelope.
- A successful Version exists before the native result is durably readable.

Uses one local public-style PDF with deterministic native text and PDF date metadata.
No seal implementation imports or source inspection; all data uses a new local DB.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import yaml
from playground_crawlresult_audit import Audit, catalog, dump, identify, readable_result, table_rows
from psycopg.rows import dict_row
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

ROOT = Path(__file__).resolve().parents[1]
TEXT = "Independent original PDF. Metadata datetime serialization must preserve native text and durable result."


def pdf_bytes():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 10 Tf 20 740 Td ({TEXT}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": "Native metadata fixture", "/Author": "Independent acceptance",
                         "/CreationDate": "D:20261013000000+00'00'", "/ModDate": "D:20261013010203+00'00'"})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        body = b"User-agent: *\nAllow: /\n" if self.path == "/robots.txt" else self.server.pdf
        self.send_response(200)
        self.send_header("Content-Type", "text/plain" if self.path == "/robots.txt" else "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def call(output, name, args, env):
    result = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, timeout=180)
    (output / f"{name}.stdout").write_text(result.stdout)
    (output / f"{name}.stderr").write_text(result.stderr)
    dump(output / f"{name}.command.json", {"argv": args, "exit_code": result.returncode})
    return result


def execute(run_name):
    work, output = ROOT / "work" / run_name, ROOT / "artifacts" / run_name
    if work.exists() or output.exists():
        raise ValueError("existing run cannot be overwritten")
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    database = "seal_" + run_name.replace("-", "_")
    if len(database) > 63 or not database.replace("_", "").isalnum():
        raise ValueError("invalid isolated database name")
    env = dict(os.environ, SEAL_DATABASE_URL=f"postgresql:///{database}", SEAL_ARCHIVE=str(work / "archive"), SEAL_ALLOW_LOOPBACK="1")
    if call(output, "createdb", ["createdb", database], env).returncode:
        raise RuntimeError("createdb failed")
    if call(output, "init", ["uv", "run", "--frozen", "seal", "init"], env).returncode:
        raise RuntimeError("init failed")
    pdf = pdf_bytes()
    (work / "original.pdf").write_bytes(pdf)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.pdf, server.requests = pdf, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/metadata.pdf"
    config = {"id": "dd-050", "entry_urls": [url], "allowed_hosts": ["127.0.0.1"], "allowed_path_prefixes": ["/"],
              "archive_approved": True, "scope": "Independent loopback PDF datetime metadata acceptance", "robots": True,
              "budget": {"requests": 10, "seconds": 120, "response_bytes": 1048576},
              "crawl4ai": {"mode": "http", "max_depth": 0, "word_count_threshold": 1}}
    source = work / "dd-050.yaml"
    source.write_text(yaml.safe_dump(config, sort_keys=False))
    try:
        result = call(output, "collect", ["uv", "run", "--frozen", "seal", "collect", str(source), "--no-progress"], env)
        receipt = json.loads(result.stdout)
        dump(output / "receipt.json", receipt)
    finally:
        server.shutdown()
        server.server_close()
    dump(output / "requests.json", server.requests)
    audit = Audit(work / "archive", output)
    digest = hashlib.sha256(pdf).hexdigest()
    audit.check("original PDF bytes are archived exactly", audit.cas(digest) == pdf)
    audit.check("PDF is fetched once through collection", server.requests.count("/metadata.pdf") == 1, server.requests)
    with psycopg.connect(env["SEAL_DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        tables = catalog(conn)
        obs = [o for o in table_rows(conn, identify(tables, {"run_id", "resource_id", "body_hash", "archive_ref", "attempt_epoch"})) if str(o["run_id"]) == receipt["run_id"] and o["url"] == url]
        versions = table_rows(conn, identify(tables, {"crawl_result", "resource_id", "observation_id"}))
        diagnostic = receipt.get("report", {}).get("crawl_results", {})
        ref = diagnostic.get(str(obs[0]["id"])) if obs else None
        audit.check("valid dated PDF has persisted observation and SDK result", bool(obs) and bool(ref), {"observations": len(obs), "diagnostics": len(diagnostic), "run_status": receipt.get("status"), "errors": receipt.get("errors")})
        if ref:
            envelope = json.loads(audit.cas(ref))
            sdk, payload = readable_result(envelope, audit)
            markdown = (payload.get("markdown") or {}).get("raw_markdown", "")
            audit.check("PDF SDK text retains original complete sentence", sdk.success and TEXT in markdown, markdown)
            metadata = payload.get("metadata") or {}
            encoded = json.dumps(metadata)
            audit.check("PDF native CreationDate has ISO JSON representation", bool(re.search(r"2026-10-13[T ]00:00:00", encoded)), metadata)
            audit.check("PDF native ModDate has ISO JSON representation", bool(re.search(r"2026-10-13[T ]01:02:03", encoded)), metadata)
            audit.check("one successful Version references readable SDK result", len(versions) == 1 and versions[0]["crawl_result"] == envelope)
            dump(output / "crawlresult.json", envelope)
        else:
            audit.check("no successful Version before result durability", not versions, len(versions))
        dump(output / "observations.json", obs)
        dump(output / "versions.json", versions)
    failures = [a for a in audit.assertions if a["status"] == "FAIL"]
    summary = {"status": "FAIL" if failures else "PASS", "assertions": len(audit.assertions), "failures": failures,
               "database": database, "artifact": str(output), "scope": "Actual dated PDF CLI / CAS / SQL / SDK round trip"}
    dump(output / "assertions.json", audit.assertions)
    dump(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_name")
    raise SystemExit(execute(parser.parse_args().run_name))
