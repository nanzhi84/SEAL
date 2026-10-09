"""Public loopback responses for planned Recheck acceptance, never production data."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Site:
    def __init__(self):
        self.ledger, self.failed, self.missing = [], set(), set()
        self.reverse = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                status, kind = 200, "text/html; charset=utf-8"
                if path in owner.failed:
                    status, body = 503, b"Temporary public source failure"
                elif path.startswith("/api/group-"):
                    group = int(path.rsplit("-", 1)[1])
                    rows = [
                        {
                            "id": str(123 + group * 2 + index),
                            "title": f"Published entity {123 + group * 2 + index}",
                            "body": "Original JSON business content",
                            "detail_url": f"{owner.url}/details/{123 + group * 2 + index}",
                        }
                        for index in range(2)
                        if str(123 + group * 2 + index) not in owner.missing
                    ]
                    if owner.reverse:
                        rows.reverse()
                    body = json.dumps(
                        {
                            "title": "Published API envelope",
                            "body": "Legacy public API document",
                            "data": {"data": {"dataList": rows}},
                        }
                    ).encode()
                    kind = "application/json"
                elif path.startswith(("/details/", "/documents/")):
                    identity = path.rsplit("/", 1)[1]
                    body = (
                        f"<h1>Published document {identity}</h1>"
                        f"<article>Original HTML business content {identity}</article>"
                    ).encode()
                else:
                    status, body = 404, b"Unknown public fixture"
                owner.ledger.append({"method": "GET", "path": self.path, "status": status})
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
