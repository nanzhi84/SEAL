"""Retained manual Collect demonstration against a controlled HTTP source.

Uses an operator-provided PostgreSQL database and archive. Never initializes,
stops or destroys either store. Synthetic content proves engineering behavior;
it is not evidence that the eight public websites are reachable or adapted.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class DemoHandler(BaseHTTPRequestHandler):
    revision = "A"
    requests = []

    def do_GET(self):
        self.requests.append(self.path)
        if self.path == "/robots.txt":
            body, content_type = b"User-agent: *\nAllow: /\n", "text/plain"
        elif self.path == "/":
            body = b'<html><title>Public notices</title><nav><a href="/notice!public.html">Notice</a></nav></html>'
            content_type = "text/html; charset=utf-8"
        else:
            body = (
                "<html><title>Controlled legal notice</title>"
                '<meta name="pubdate" content="2026-10-10">'
                "<article><h1>Controlled legal notice</h1><h2>第一条 公开说明</h2>"
                "<p>本模拟站点用于证明真实执行的采集、归档、结构化保存与查询机制。"
                "文本来自受控 HTTP 响应，不代表任何外部网站已经适配或已经完整覆盖。</p>"
                f"<p>Revision {self.revision}. All archived versions remain traceable.</p>"
                '<ol start="3"><li>公开条款与编号。</li><li>历史记录仍然保留。</li></ol>'
                "<table><tr><th>编号</th><th>内容</th></tr><tr><td>1</td><td>公开记录</td></tr></table>"
                "</article></html>"
            ).encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not os.environ.get("SEAL_DATABASE_URL") or not os.environ.get("SEAL_ARCHIVE"):
        parser.error("operator-provided SEAL_DATABASE_URL and SEAL_ARCHIVE are required")
    args.output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, SEAL_ALLOW_LOOPBACK="1", SEAL_USE_ENV_PROXY="0")
    receipts = []

    def cli(*parts):
        process = subprocess.run(
            [sys.executable, "-m", "seal", *map(str, parts)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        value = json.loads(process.stdout)
        receipts.append(
            {"command": list(map(str, parts)), "exit": process.returncode, "result": value}
        )
        if process.returncode:
            raise RuntimeError(value)
        return value

    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cli("db", "migrate")
        source = "v13-retained-demo"
        config = {
            "id": source,
            "entry_urls": [f"http://127.0.0.1:{server.server_port}/"],
            "allowed_hosts": ["127.0.0.1"],
            "allowed_path_prefixes": ["/"],
            "identity": "business_key",
            "output_schema": "record.v1",
            "archive_approved": True,
            "scope": "Controlled public loopback fixture; retained engineering demonstration",
            "robots": True,
            "delay": 0.0,
            "concurrency": 1,
            "budget": {"requests": 20, "seconds": 30, "response_bytes": 100000},
        }
        config_path, params_path = args.output / "source.json", args.output / "params.json"
        config_path.write_text(json.dumps(config))
        params_path.write_text(json.dumps({"discover_sitemaps": False}))
        cli("source", "apply", config_path)
        version = cli("recipe", "pack", "recipes/seeded")["recipe_version"]
        binding = cli("binding", "create", source, "--recipe", version, "--params", params_path)[
            "binding_id"
        ]
        runs = [cli("collect", source, "--binding", binding) for _ in range(2)]
        first_records = cli("export", source, "--run", runs[0]["run_id"])["records"]
        assert len(first_records) == 1, first_records
        record_id = first_records[0]["record_id"]
        assert len(cli("inspect", "record", record_id)["versions"]) == 1
        DemoHandler.revision = "B"
        runs.append(cli("collect", source, "--binding", binding))
        record = cli("inspect", "record", record_id)
        assert len(record["versions"]) == 2, record
        count = len(DemoHandler.requests)
        replay = cli("replay", runs[-1]["run_id"])
        assert len(DemoHandler.requests) == count, "Replay made a network request"
        inspected = cli("inspect", "run", runs[-1]["run_id"])
        snapshot_id = next(
            i["snapshot_id"]
            for i in inspected["run"]["inputs"]
            if i["role"] != "robots" and "notice!" in i["logical_url"]
        )
        snapshot = cli("inspect", "snapshot", snapshot_id)
        assert snapshot["archive"]["availability"] == "available", snapshot
        summary = {
            "status": "PASS",
            "source": source,
            "binding_id": binding,
            "recipe_version": version,
            "run_ids": [run["run_id"] for run in runs],
            "replay_run_id": replay["run_id"],
            "record_id": record_id,
            "snapshot_id": snapshot_id,
            "record_count": len(first_records),
            "version_count": len(record["versions"]),
            "replay_network_requests": 0,
            "archive": env["SEAL_ARCHIVE"],
            "database_retained": True,
            "scope": "Controlled HTTP fixture; actual retained PostgreSQL and archive, no external adaptation claim",
        }
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary))
    finally:
        server.shutdown()
        server.server_close()
        (args.output / "receipts.json").write_text(
            json.dumps(receipts, indent=2, default=str) + "\n"
        )
        (args.output / "requests.json").write_text(
            json.dumps(DemoHandler.requests, indent=2) + "\n"
        )


if __name__ == "__main__":
    main()
