"""真实 HTTP/PDF 输入；只产生合成、无敏感信息的业务内容。"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def pdf_bytes(text: str) -> bytes:
    """生成一个可由独立 PDF reader 验证的单页文本 PDF。"""
    stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, content in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + content + b"\nendobj\n")
    xref = len(result)
    result.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)


def html_bytes(title: str, body: str, links: tuple[str, ...] = ()) -> bytes:
    anchors = "".join(f'<a href="{url}">{url}</a>' for url in links)
    return (
        "<!doctype html><html><head><title>" + title + "</title></head><body><main>"
        "<h1>" + title + "</h1><p>" + body + "</p>" + anchors + "</main></body></html>"
    ).encode()


class Fixture:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.log = directory / "requests.jsonl"
        self.lock = threading.Lock()
        self.responses: dict[str, tuple[int | str, str, bytes]] = {}
        self.delay: dict[str, float] = {}
        self.headers: dict[str, dict[str, str]] = {}
        self.requests: list[dict] = []
        self.links = ("/article?edition=1", "/article?edition=2", "/gone", "/file.pdf")
        self.root_links: dict[str, tuple[str, ...]] = {"/a/": self.links, "/b/": self.links}
        self.set("/article?edition=1", body=html_bytes("Article One", "Alpha original content."))
        self.set("/article?edition=2", body=html_bytes("Article Two", "Second query has meaning."))
        self.set("/gone", body=html_bytes("Orphan", "Known resource must remain discoverable."))
        self.set("/file.pdf", mime="application/pdf", body=pdf_bytes("SEAL PDF original sample"))
        self.set("/added", body=html_bytes("Added", "New round discovers this resource."))
        self.set("/after-crash", body=html_bytes("Recovery sibling", "Discovered before crash and fetched after recovery."))
        self.set("/retry", body=html_bytes("Retry", "Same bytes must be parsed again."))
        self.set("/slow", body=html_bytes("Slow", "Delayed response used for crash recovery."))
        self.set("/outside", body=html_bytes("Outside", "Out of permitted path."))
        self.set("/robots-denied", body=html_bytes("Robots denied", "Must not be fetched."))
        self.set("/robots.txt", mime="text/plain", body=b"User-agent: *\nDisallow: /robots-denied\n")
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path in fixture.root_links:
                    response = (200, "text/html", html_bytes("Public index", "Source index.", fixture.root_links[self.path]))
                else:
                    response = fixture.responses.get(self.path, (404, "text/plain", b"missing"))
                status, mime, body = response
                record = {
                    "path": self.path, "at": time.time(), "status": status,
                    "mime": mime, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                }
                with fixture.lock:
                    fixture.requests.append(record)
                    with fixture.log.open("a") as stream:
                        stream.write(json.dumps(record) + "\n")
                time.sleep(fixture.delay.get(self.path, 0))
                if status == "network":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Volatile-Request", str(time.time_ns()))
                for key, value in fixture.headers.get(self.path, {}).items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def set(self, path: str, *, status: int | str = 200, mime="text/html", body: bytes):
        self.responses[path] = (status, mime, body)
        snapshot = self.directory / (hashlib.sha256(path.encode()).hexdigest()[:12] + ".bin")
        snapshot.write_bytes(body)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def count(self, path: str) -> int:
        with self.lock:
            return sum(row["path"] == path for row in self.requests)

    def wait_for(self, path: str, minimum: int, timeout=30):
        deadline = time.monotonic() + timeout
        while self.count(path) < minimum:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"fixture never received {path}, minimum {minimum}")
            time.sleep(0.1)
