#!/usr/bin/env python3
"""Fresh, isolated real CLI acceptance for success/failure diagnostic persistence.

Failure modes and boundaries are declared before exercising the changed application:
- Short successful HTML or failed SDK outcomes vanish from run diagnostics.
- A password challenge or HTTP 503 becomes a successful content Version.
- Duplicate seeds or a restored prior body produce duplicate versions.
- Diagnostics reused across runs refer to a different body, URL, or Observation.
- Results cannot be reconstructed by the independent SDK oracle in a new process.

Four actual CLI runs: A/B -> password-blocked A / changed B -> original A/B ->
changed A / 503 B. Only local fixture traffic is used; production data is untouched.
Each run is a new process. Database and files are retained for repeatable inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import yaml
from playground_crawlresult_audit import Audit, catalog, dump, identify, inspect_inputs, table_rows
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
STATE = {"stage": 0, "requests": []}
PARAGRAPH = "本页用于独立业务验收。归档必须对应本次实际输入，成功结果应完整保留正文、Markdown、链接与表格。失败结果必须可以在新进程读取，并且不能推进成功版本。"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        STATE["requests"].append({"stage": STATE["stage"], "path": self.path, "time": time.time()})
        stage = STATE["stage"]
        if self.path == "/robots.txt":
            code, body, kind = 200, b"User-agent: *\nAllow: /\n", "text/plain"
        elif self.path == "/a" and stage == 1:
            code, kind = 200, "text/html; charset=utf-8"
            body = b'<html><head><title>Sign in</title></head><body><p>Please authenticate.</p><input type="password"></body></html>'
        elif self.path == "/b" and stage == 3:
            code, kind = 503, "text/html; charset=utf-8"
            body = b"<html><body><p>Service temporarily unavailable.</p></body></html>"
        elif self.path in {"/a", "/b"}:
            code, kind = 200, "text/html; charset=utf-8"
            marker = "A-CONTENT-2" if self.path == "/a" and stage == 3 else "A-CONTENT-1" if self.path == "/a" else "B-CONTENT-2" if stage in {1, 2} else "B-CONTENT-1"
            body = (f'<html><head><title>{marker}</title></head><body><main><h1>{marker}</h1>'
                    f'<p>{PARAGRAPH}</p><p>{PARAGRAPH}</p><table><thead><tr><th>字段</th><th>值</th></tr></thead>'
                    f'<tbody><tr><td>input</td><td>{marker}</td></tr></tbody></table>'
                    '<a href="/a">A reference</a><a href="/b">B reference</a>'
                    '</main></body></html>').encode()
        else:
            code, body, kind = 404, b"not found", "text/plain"
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def command(output, name, argv, env):
    result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    (output / f"{name}.stdout").write_text(result.stdout)
    (output / f"{name}.stderr").write_text(result.stderr)
    dump(output / f"{name}.command.json", {"argv": argv, "exit_code": result.returncode})
    return result


def execute(run_name):
    output = ROOT / "artifacts" / run_name
    work = ROOT / "work" / run_name
    if output.exists() or work.exists():
        raise ValueError("run directories already exist; choose a new run name")
    output.mkdir(parents=True)
    work.mkdir(parents=True)
    database = "seal_" + run_name.replace("-", "_")
    if not database.replace("_", "").isalnum() or len(database) > 63:
        raise ValueError("invalid isolated database name")
    env = dict(os.environ, SEAL_DATABASE_URL=f"postgresql:///{database}",
               SEAL_ARCHIVE=str(work / "archive"), SEAL_ALLOW_LOOPBACK="1")
    result = command(output, "createdb", ["createdb", database], env)
    if result.returncode:
        raise RuntimeError("could not create a fresh isolated database")
    result = command(output, "init", ["uv", "run", "--frozen", "seal", "init"], env)
    if result.returncode:
        raise RuntimeError("seal init failed; retained command evidence")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    config = {"id": "dd-017", "entry_urls": [base + "/a", base + "/a", base + "/b"],
              "allowed_hosts": ["127.0.0.1"], "allowed_path_prefixes": ["/"],
              "archive_approved": True, "scope": "Isolated loopback diagnostic acceptance",
              "robots": True, "recheck_seconds": 0, "concurrency": 1,
              "budget": {"requests": 20, "seconds": 120, "response_bytes": 1048576},
              "crawl4ai": {"mode": "http", "max_depth": 0, "word_count_threshold": 1}}
    source = work / "dd-017.yaml"
    source.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    dump(output / "environment.json", {"database": database, "python": platform.python_version(),
                                      "archive": str(work / "archive"), "scope": "loopback only"})
    receipts = []
    try:
        for stage in range(4):
            STATE["stage"] = stage
            result = command(output, f"stage-{stage}", ["uv", "run", "--frozen", "seal", "collect",
                             str(source), "--no-progress"], env)
            receipt = json.loads(result.stdout)
            receipts.append(receipt)
            dump(output / f"stage-{stage}-receipt.json", receipt)
    finally:
        server.shutdown()
        server.server_close()
        dump(output / "fixture-requests.json", STATE["requests"])
    return audit_fixture(output, work, database, base, receipts)


def audit_fixture(output, work, database, base, receipts):
    audit = Audit(work / "archive", output / "independent")
    stages = []
    with psycopg.connect(f"postgresql:///{database}", row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        tables = catalog(conn)
        dump(output / "catalog.json", tables)
        run_table = identify(tables, {"source_id", "config_hash", "report", "attempt_epoch"})
        obs_table = identify(tables, {"run_id", "resource_id", "body_hash", "archive_ref", "attempt_epoch"})
        version_table = identify(tables, {"crawl_result", "resource_id", "observation_id"})
        runs = {str(r["id"]): r for r in table_rows(conn, run_table)}
        all_obs = table_rows(conn, obs_table)
        versions = {str(v["id"]): v for v in table_rows(conn, version_table)}
        for stage, receipt in enumerate(receipts):
            run_id = str(receipt["run_id"])
            obs = [o for o in all_obs if str(o["run_id"]) == run_id]
            rows = inspect_inputs(audit, f"stage-{stage}", runs[run_id], obs, versions)
            business_rows = [r for r in rows if r["observation"]["url"] in {base + "/a", base + "/b"}]
            audit.check(f"stage-{stage} exact duplicate-seed-safe inputs", len(business_rows) == 2,
                        [r["observation"]["url"] for r in business_rows])
            stages.append({r["observation"]["url"]: r for r in business_rows})
        a, b = base + "/a", base + "/b"
        audit.check("password challenge has no Version", stages[1][a]["observation"]["version_id"] is None)
        audit.check("restored prior A reuses Version", stages[2][a]["observation"]["version_id"] == stages[0][a]["observation"]["version_id"])
        audit.check("stable B after change reuses Version", stages[2][b]["observation"]["version_id"] == stages[1][b]["observation"]["version_id"])
        audit.check("503 B has persisted failed SDK result", stages[3][b]["crawl_result_available"] and not stages[3][b]["result"]["success"])
        audit.check("503 B has no Version", stages[3][b]["observation"]["version_id"] is None)
        audit.check("changed successful A advances Version", stages[3][a]["observation"]["version_id"] != stages[0][a]["observation"]["version_id"])
        audit.check("only successful changes produce four Versions", len(versions) == 4, len(versions))
        for row in stages[0].values():
            summary = row["result"]
            audit.check("fixture successful Markdown has original paragraph", PARAGRAPH in summary["raw_markdown"])
            audit.check("fixture successful links retain both targets", summary["links"].get("internal", 0) == 2)
        dump(output / "sql-observations.json", all_obs)
        dump(output / "sql-versions.json", list(versions.values()))
        dump(output / "sql-runs.json", list(runs.values()))
    failures = [a for a in audit.assertions if a["status"] == "FAIL"]
    summary = {"status": "FAIL" if failures else "PASS", "assertions": len(audit.assertions),
               "failures": failures, "database": database, "artifact": str(output),
               "unverified": ["Worker crash checkpoint retry is not exercised by these four CLI rounds."]}
    dump(output / "assertions.json", audit.assertions)
    dump(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_name")
    parser.add_argument("--audit-existing", action="store_true")
    args = parser.parse_args()
    if args.audit_existing:
        output, work = ROOT / "artifacts" / args.run_name, ROOT / "work" / args.run_name
        database = json.loads((output / "environment.json").read_text())["database"]
        base = yaml.safe_load((work / "dd-017.yaml").read_text())["entry_urls"][0].removesuffix("/a")
        receipts = [json.loads((output / f"stage-{stage}-receipt.json").read_text()) for stage in range(4)]
        raise SystemExit(audit_fixture(output, work, database, base, receipts))
    raise SystemExit(execute(args.run_name))
