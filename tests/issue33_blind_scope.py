"""Independent contract check defined before execution.

Failure boundaries: changing only Adapter planning limits/conditions must preserve existing
Resource IDs, Version IDs and fingerprints when the received document bytes are unchanged;
repeated candidates in one response must deliver once and retain valid persistent task keys.
This test uses only the real CLI, SQL and a complete-body loopback source.
"""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml
from issue33_blind_acceptance import ART, ROOT, WORK, cli, query, reproduction_command, setup


def main():
    phase = "scope"
    (WORK / f"{phase}-state.json").unlink(missing_ok=True)
    env = setup(phase)
    cli(env, ["init"], "init", phase)
    text = (
        "Article 1. Every regulated authority shall retain a complete original decision and "
        "publish the record. Article 2. This rule enters into force on 20 October 2026. "
        "Article 3. The competent authority shall supervise implementation annually."
    )
    documents = [
        {"document_number": "2026-91001", "title": "Complete rule A", "type": "Rule", "body": text},
        {
            "document_number": "2026-91002",
            "title": "Complete rule B",
            "type": "Rule",
            "body": text.replace("annually", "monthly"),
        },
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps(
                {"count": 2, "results": [documents[0], documents[0], documents[1]]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "id": "dd-499",
        "adapter": "federal_register",
        "adapter_options": {"max_pages": 1, "per_page": 2, "listing_body_field": "body"},
        "entry_urls": [f"http://127.0.0.1:{server.server_port}/api"],
        "allowed_hosts": ["127.0.0.1"],
        "allowed_path_prefixes": ["/"],
        "archive_approved": True,
        "scope": "Isolated planning-limit/replay acceptance fixture.",
        "robots": False,
        "delay": 0.0,
        "concurrency": 2,
        "budget": {"requests": 20, "seconds": 120, "response_bytes": 10485760, "documents": 10},
        "metadata": {
            "source_name": "Independent complete-body fixture",
            "document_type": "法律法规",
            "language": "en",
        },
    }
    path = WORK / phase / "dd-499.yaml"
    checks = []
    statement = (
        "SELECT r.id,r.external_key,r.current_version_id,v.input_fingerprint,"
        "v.parser_fingerprint,v.content_hash,(SELECT count(*) FROM seal.version vv "
        "WHERE vv.resource_id=r.id) AS versions FROM seal.resource r "
        "JOIN seal.version v ON v.id=r.current_version_id ORDER BY r.external_key"
    )
    before = None
    try:
        for label, change in [
            ("baseline", {}),
            ("max-pages-only", {"max_pages": 2}),
            ("per-page-only", {"per_page": 100}),
            ("conditions-only", {"conditions": {"type": ["RULE"]}}),
        ]:
            config["adapter_options"].update(change)
            path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
            (ART / phase / f"{label}-source.yaml").write_text(path.read_text())
            result, rc = cli(env, ["collect", str(path), "--no-progress"], label, phase)
            rows = query(env, statement)
            assert rc == 0 and result["status"] == "complete", result
            assert len(rows) == 2 and all(r["versions"] == 1 for r in rows), rows
            if before is None:
                before = rows
            else:
                assert rows == before, (label, before, rows)
            tasks = query(env, "SELECT key FROM seal.task WHERE run_id=%s", (result["run_id"],))
            assert all(
                len(t["key"]) == 64 and all(c in "0123456789abcdef" for c in t["key"])
                for t in tasks
            )
            checks.append(
                {
                    "scenario": label,
                    "passed": True,
                    "resources": rows,
                    "persistent_task_keys_sha256": True,
                    "run_id": result["run_id"],
                }
            )
    finally:
        server.shutdown()
        server.server_close()
    report = {
        "status": "PASS",
        "checks": checks,
        "test_sha256": hashlib.sha256(
            (ROOT / "tests/issue33_blind_scope.py").read_bytes()
        ).hexdigest(),
        "command": reproduction_command("issue33_blind_scope.py"),
    }
    (ART / phase / "assertions.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {"status": "PASS", "scenarios": len(checks), "stable_versions": True},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
