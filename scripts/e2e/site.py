"""Loopback-only authorized synthetic site; independent expected documents."""

import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def pdf_bytes():
    # Fixed, text-layer PDF; the oracle is independent of pypdf extraction.
    stream = b"BT /F1 18 Tf 50 750 Td (Synthetic PDF notice) Tj ET"
    parts = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for i, part in enumerate(parts, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + part + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{n:010d} 00000 n \n".encode() for n in offsets[1:])
    return data + f"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


class Site:
    def __init__(self):
        self.version = "A"
        self.broken = False
        self.failure = None
        self.ledger = []
        self.delay_next = False
        self.delay_started = threading.Event()
        self.delay_release = threading.Event()
        self.pdf = pdf_bytes()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                path = urlsplit(self.path).path
                status, headers = 200, {"Content-Type": "text/html; charset=utf-8"}
                body = b""
                if path == "/robots.txt":
                    body = b"User-agent: *\nAllow: /\n"
                elif path.endswith("/list"):
                    prefix = path.split("/")[1]
                    body = f'<div class="documents"><a href="/{prefix}/one">one</a></div><a class="next" href="/{prefix}/page2">Next</a>'.encode()
                elif path.endswith("/page2"):
                    prefix = path.split("/")[1]
                    body = f'<div class="documents"><a href="/{prefix}/two">two</a></div>'.encode()
                    if owner.failure == "page":
                        status = 503
                elif path.endswith(("/one", "/two")):
                    title = "First notice" if path.endswith("one") else "Second notice"
                    tag = "section" if owner.broken and path.startswith("/a/") else "article"
                    body = f"<h1>{title}</h1><time>2026-01-02</time><{tag}>Public content {owner.version}</{tag}>".encode()
                    if owner.failure == "empty":
                        body = b"<h1>Login</h1><article></article>"
                    elif owner.failure == "sensitive":
                        body += b" access_token=synthetic-secret-do-not-archive"
                elif path == "/probe/pdf":
                    body, headers["Content-Type"] = owner.pdf, "application/pdf"
                elif path == "/probe/json":
                    body = b'{"title":"JSON notice","body":"Public JSON body","date":"2026-01-02"}'
                    headers["Content-Type"] = "application/json"
                elif path == "/probe/txt":
                    body, headers["Content-Type"] = (
                        b"Text notice\nPublic text body",
                        "text/plain; charset=utf-8",
                    )
                elif path in (
                    "/probe/redirect",
                    "/probe/meta",
                    "/probe/retry",
                    "/probe/gzip",
                    "/probe/deflate",
                ):
                    body = b"<h1>Probe notice</h1><article>Probe content</article>"
                    if path.endswith("redirect"):
                        status, headers["Location"] = 302, "/probe/gzip"
                    elif path.endswith("meta"):
                        body = b'<meta http-equiv="refresh" content="0;url=/probe/gzip">'
                    elif (
                        path.endswith("retry") and sum(r["path"] == path for r in owner.ledger) < 2
                    ):
                        status = 503
                    if path.endswith(("gzip", "redirect")):
                        headers["Content-Encoding"], body = "gzip", gzip.compress(body)
                    if path.endswith("deflate"):
                        import zlib

                        headers["Content-Encoding"], body = "deflate", zlib.compress(body)
                elif path == "/probe/429":
                    status, headers["Retry-After"] = 429, "120"
                elif path == "/probe/304":
                    status = 304
                elif path == "/probe/large":
                    body = b"x" * 200000
                elif path == "/probe/badpdf":
                    body, headers["Content-Type"] = b"broken PDF", "application/pdf"
                elif path.startswith("/cache/"):
                    count = sum(r["path"] == path for r in owner.ledger)
                    body = b"public cache fixture"
                    if path.endswith(("rfc", "fallback")):
                        headers.update(
                            {"Cache-Control": "public, max-age=0", "ETag": '"fixture-v1"'}
                        )
                        if count:
                            if path.endswith("fallback"):
                                owner.ledger.append(
                                    {
                                        "method": self.command,
                                        "path": path,
                                        "status": "connection_closed",
                                    }
                                )
                                self.close_connection = True
                                return
                            status, body = 304, b""
                    else:
                        status = 500
                else:
                    status = 404
                owner.ledger.append({"method": self.command, "path": path, "status": status})
                if owner.delay_next and path.endswith("/one"):
                    owner.delay_next = False
                    owner.delay_started.set()
                    owner.delay_release.wait(timeout=12)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Set-Cookie", "fixture-cookie=must-not-persist")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
