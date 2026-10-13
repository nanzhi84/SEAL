#!/usr/bin/env python3
"""Independent real CLI robots behavior acceptance, written before the proposed fix.

Recognizable failures and boundaries:
- Broad Disallow:/data/ incorrectly wins over more specific Allow:/data/upload/.
- The allowance accidentally opens /data/private/ or fetching happens before policy.
- robots.txt 403 is mislabeled robots_denied by unrelated SDK traceback text.
- Policy and business requests advertise different identities despite Source user_agent.
- Unavailable policy permits target fetching, or rejected input creates a Version.

Two loopback servers and isolated Source IDs exercise the actual CLI and SQL/CAS.
No implementation imports, production DB, prior test code, or public-site fetches.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import yaml
from playground_crawlresult_audit import Audit, catalog, dump, identify, readable_result, table_rows
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = "SEAL/independent-robots-acceptance"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.requests.append({"path": self.path, "user_agent": self.headers.get("User-Agent")})
        if self.path == "/robots.txt":
            status = 403 if self.server.policy_forbidden else 200
            body = (b"Policy service unavailable" if self.server.policy_forbidden else
                    b"User-agent: *\nDisallow: /data/\nAllow: /data/upload/\n")
            kind = "text/plain"
        else:
            status, kind = 200, "text/html; charset=utf-8"
            body = (f"<html><head><title>Allowed original</title></head><body><main><h1>Allowed original</h1>"
                    f"<p>THIS IS THE ORIGINAL ALLOWED DOCUMENT AT {self.path}. "
                    "The complete public paragraph must be retained after policy evaluation and must "
                    "correspond to the same input archived by the real collection process.</p></main></body></html>").encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
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
        raise ValueError("existing run name cannot be overwritten")
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    database = "seal_" + run_name.replace("-", "_")
    if len(database) > 63 or not database.replace("_", "").isalnum():
        raise ValueError("invalid isolated database name")
    env = dict(os.environ, SEAL_DATABASE_URL=f"postgresql:///{database}",
               SEAL_ARCHIVE=str(work / "archive"), SEAL_ALLOW_LOOPBACK="1")
    if call(output, "createdb", ["createdb", database], env).returncode:
        raise RuntimeError("createdb failed")
    if call(output, "init", ["uv", "run", "--frozen", "seal", "init"], env).returncode:
        raise RuntimeError("init failed")
    receipts, servers = [], []
    try:
        for source, forbidden in (("dd-050", False), ("dd-468", True)):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.policy_forbidden, server.requests = forbidden, []
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
            base = f"http://127.0.0.1:{server.server_port}"
            entries = [base + "/document"] if forbidden else [base + "/data/upload/allowed.html", base + "/data/private/denied.html"]
            config = {"id": source, "entry_urls": entries, "allowed_hosts": ["127.0.0.1"],
                      "allowed_path_prefixes": ["/"], "archive_approved": True,
                      "scope": "Independent loopback robots acceptance", "robots": True,
                      "user_agent": IDENTITY, "concurrency": 1,
                      "budget": {"requests": 10, "seconds": 120, "response_bytes": 1048576},
                      "crawl4ai": {"mode": "http", "max_depth": 0, "word_count_threshold": 1}}
            source_path = work / f"{source}.yaml"
            source_path.write_text(yaml.safe_dump(config, sort_keys=False))
            result = call(output, source, ["uv", "run", "--frozen", "seal", "collect", str(source_path), "--no-progress"], env)
            receipt = json.loads(result.stdout)
            receipts.append((source, base, receipt))
            dump(output / f"{source}-receipt.json", receipt)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        dump(output / "requests.json", {source: server.requests for (source, _, _), server in zip(receipts, servers, strict=False)})
    audit = Audit(work / "archive", output)
    with psycopg.connect(env["SEAL_DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        tables = catalog(conn)
        observations = table_rows(conn, identify(tables, {"run_id", "resource_id", "body_hash", "archive_ref", "attempt_epoch"}))
        for (source, base, receipt), server in zip(receipts, servers, strict=True):
            obs = [o for o in observations if str(o["run_id"]) == receipt["run_id"]]
            by_url = {o["url"]: o for o in obs}
            audit.check(f"{source} policy requests use configured Source identity",
                        all(r["user_agent"] == IDENTITY for r in server.requests if r["path"] == "/robots.txt"),
                        [r for r in server.requests if r["path"] == "/robots.txt"])
            audit.check(f"{source} every business request uses same identity",
                        all(r["user_agent"] == IDENTITY for r in server.requests), server.requests)
            if source == "dd-050":
                allowed = by_url[base + "/data/upload/allowed.html"]
                denied = by_url[base + "/data/private/denied.html"]
                audit.check("longest Allow grants upload content", allowed["version_id"] is not None,
                            {"outcome": allowed["outcome"], "error": allowed["error"]})
                audit.check("broader Disallow still denies private content",
                            denied["version_id"] is None and denied["error"] == "robots_denied")
                audit.check("denied private URL never reaches HTTP fixture",
                            not any(r["path"] == "/data/private/denied.html" for r in server.requests))
                ref = receipt["report"].get("crawl_results", {}).get(str(allowed["id"]))
                if ref:
                    result, payload = readable_result(json.loads(audit.cas(ref)))
                    audit.check("allowed upload SDK result contains original body",
                                result.success and "THIS IS THE ORIGINAL ALLOWED DOCUMENT" in
                                (payload.get("markdown") or {}).get("raw_markdown", ""))
                else:
                    audit.check("allowed upload SDK result contains original body", False, "No SDK result")
            else:
                document = by_url[base + "/document"]
                audit.check("policy 403 preserves robots_unavailable diagnostic",
                            document["error"] == "robots_unavailable", {"error": document["error"], "outcome": document["outcome"]})
                audit.check("policy 403 cannot fetch business URL", all(r["path"] == "/robots.txt" for r in server.requests))
                audit.check("policy 403 cannot create business Version", document["version_id"] is None)
            dump(output / f"{source}-observations.json", obs)
    failed = [a for a in audit.assertions if a["status"] == "FAIL"]
    summary = {"status": "FAIL" if failed else "PASS", "assertions": len(audit.assertions),
               "failures": failed, "database": database, "artifact": str(output),
               "scope": "Longest path policy rules, same explicit identity, exact 403 policy diagnostic"}
    dump(output / "assertions.json", audit.assertions)
    dump(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(failed))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_name")
    raise SystemExit(execute(parser.parse_args().run_name))
