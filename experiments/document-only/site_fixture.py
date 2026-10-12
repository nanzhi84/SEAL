"""Bounded synthetic public HTTP, deliberately independent of Runtime parsing."""

import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from adapter_fixture import resources as adapter_resources
from adapter_fixture import response as adapter_response
from fixtures import docx_bytes, pdf_bytes, resources


class Site:
    def __init__(self):
        self.resources = resources(
            pdf_bytes(),
            docx_bytes(),
            Path("experiments/v1.2-acceptance/fixtures/public-structured.xls").read_bytes(),
        )
        self.resources.update(
            {
                "/documents/empty.txt": ("text/plain", b""),
                "/documents/scanned.pdf": (
                    "application/pdf",
                    pdf_bytes().replace(b"(Synthetic PDF notice) Tj", b"(                    ) Tj"),
                ),
                "/documents/root": (
                    "text/html",
                    b"<html><h1>All resources</h1>"
                    + b"".join(
                        f'<a href="{p}">{p}</a>'.encode()
                        for p in [
                            "/documents/notice",
                            "/documents/also",
                            "/documents/page2",
                            "/documents/retry",
                            "/documents/redirect",
                        ]
                    )
                    + b"</html>",
                ),
                "/documents/slow": ("text/plain", b"Slow public text"),
                "/documents/compressed.txt": ("text/plain", b"Compressed public text"),
            }
        )
        self.ledger = []
        self.resources.update(adapter_resources())
        self.nonce = 0
        self.retry_count = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                form = parse_qs(
                    self.rfile.read(int(self.headers["Content-Length"])).decode(),
                    keep_blank_values=True,
                )
                self.respond(form)

            def respond(self, form=None):
                parsed = urlsplit(self.path)
                path = parsed.path
                owner.ledger.append({"method": "GET", "path": self.path})
                owner.ledger[-1]["method"] = self.command
                owner.nonce += 1
                adapted = adapter_response(path, parsed.query, owner.nonce, form)
                if path == "/documents/slow":
                    time.sleep(3)
                if path == "/documents/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/documents/plain.txt")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/documents/retry":
                    owner.retry_count += 1
                    if owner.retry_count == 1:
                        self.send_response(503)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    content_type, body = "text/plain", b"Recovered public text"
                else:
                    content_type, body = adapted or owner.resources.get(
                        path, ("text/plain", b"Missing")
                    )
                self.send_response(
                    200 if adapted or path in owner.resources or path == "/documents/retry" else 404
                )
                if path == "/documents/compressed.txt":
                    body = gzip.compress(body, mtime=0)
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
