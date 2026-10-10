"""Bounded loopback fixtures; no assertion here depends on a live public source."""

import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from attachment_fixtures import docx_bytes, xls_bytes
from sensitive_fixtures import body_for


def rows():
    return [
        {
            "id": f"entity-{i}",
            "name": f"Public entity {i}",
            "active": i % 2 == 0,
            "count": i,
            "score": i + 0.5,
            "optional": None,
            "tags": ["public", str(i)],
            "address": {"city": "Singapore", "rank": i},
            "a/b~c": f"Escaped key {i}",
        }
        for i in range(10)
    ]


class Site:
    def __init__(self):
        from e2e.site import pdf_bytes

        self.ledger = []
        self.state = "base"
        self.delays = {}
        self.hold_next = False
        self.hold_started = threading.Event()
        self.hold_release = threading.Event()
        self.pdf = pdf_bytes()
        self.xls = xls_bytes()
        self.docx = docx_bytes()
        self.partial_docx = docx_bytes(unsupported=True)
        self.structured_broken = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                path, query = parsed.path, parse_qs(parsed.query)
                time.sleep(owner.delays.get(path, 0))
                status, content_type = 200, "text/html; charset=utf-8"
                if path.startswith("/sensitive/"):
                    body = body_for(path, owner.state)
                elif path.startswith("/api"):
                    data = copy.deepcopy(rows())
                    if owner.state == "reorder":
                        data.reverse()
                    elif owner.state == "changed":
                        data[3]["count"] = 33
                    elif owner.state == "missing":
                        data.pop()
                    elif owner.state == "conflict":
                        alternate = copy.deepcopy(data[0])
                        alternate["name"] = "Conflicting entity"
                        data.append(alternate)
                    elif owner.state == "malformed":
                        data.append({"name": "Row without a stable business key"})
                    if path == "/api/left":
                        data = data[:5]
                    elif path == "/api/right":
                        data = [data[0], *data[5:]]
                    if query.get("empty") == ["1"]:
                        data = []
                    body = json.dumps({"data": {"data": {"dataList": data}}}).encode()
                    content_type = "application/json"
                elif path == "/html/list":
                    body = b'<div class="documents"><a href="/html/one">one</a><a href="/html/one">duplicate</a></div><a class="next" href="/html/page2">Next</a>'
                elif path == "/html/page2":
                    body = b'<div class="documents"><a href="/html/one">another parent</a><a href="/html/two">two</a></div>'
                elif path == "/html/loop":
                    body = b'<div class="documents"><a href="/html/one">one</a></div><a class="next" href="/html/loop">Next</a>'
                elif path == "/html/budget":
                    body = (
                        '<div class="documents">'
                        + "".join(f'<a href="/html/detail-{i}">{i}</a>' for i in range(100))
                        + "</div>"
                    ).encode()
                elif path == "/html/outside":
                    body = b'<div class="documents"><a href="/denied/one">outside</a><a href="/html/one">inside</a></div>'
                elif path == "/assets/list":
                    body = b'<h1>Business documents</h1><iframe src="/assets/frame"></iframe><a class="attachment" href="/assets/text.pdf">Published PDF</a><a class="attachment" href="/assets/data.csv">Published CSV</a>'
                elif path == "/assets/structured-list":
                    body = b'<a class="attachment" href="/assets/public.xls">Public workbook</a><a class="attachment" href="/assets/public.docx">Public document</a>'
                elif path == "/assets/public.xls":
                    body = b"damaged synthetic BIFF" if owner.structured_broken else owner.xls
                    content_type = "application/vnd.ms-excel"
                elif path == "/assets/public.docx":
                    body = b"damaged synthetic OOXML" if owner.structured_broken else owner.docx
                    content_type = (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    )
                elif path == "/assets/partial-list":
                    body = b'<a class="attachment" href="/assets/partial.docx">Document with unsupported content</a>'
                elif path == "/assets/partial.docx":
                    body = owner.partial_docx
                    content_type = (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    )
                elif path == "/assets/bad-list":
                    body = b'<a class="attachment" href="/assets/bad.pdf">Damaged PDF</a><a class="attachment" href="/assets/table.xlsx">Unsupported workbook</a>'
                elif path == "/assets/frame":
                    body = b"<h1>Static iframe notice</h1><article>Public iframe business content</article>"
                elif path == "/assets/text.pdf":
                    body, content_type = owner.pdf, "application/pdf"
                elif path == "/assets/data.csv":
                    body, content_type = (
                        b"name,amount\nPublic entity,42\n",
                        "text/csv; charset=utf-8",
                    )
                elif path == "/assets/bad.pdf":
                    body, content_type = b"damaged synthetic PDF bytes", "application/pdf"
                elif path == "/assets/table.xlsx":
                    body, content_type = (
                        b"unsupported synthetic workbook bytes",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                elif path == "/html/parse-failed":
                    body = b"<h1>Missing body</h1>"
                elif path == "/html/download-failed":
                    owner.ledger.append(
                        {"method": "GET", "path": self.path, "status": "connection_closed"}
                    )
                    self.close_connection = True
                    return
                elif path.startswith(("/html/", "/denied/")):
                    title = "First notice" if path.endswith("one") else "Second notice"
                    body = f"<h1>{title}</h1><article>Public HTML content</article>".encode()
                else:
                    status, body = 404, b"Unknown synthetic path"
                owner.ledger.append({"method": "GET", "path": self.path, "status": status})
                if owner.hold_next and path == "/api/fence":
                    owner.hold_next = False
                    owner.hold_started.set()
                    owner.hold_release.wait(timeout=15)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
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
        self.hold_release.set()
        self.server.shutdown()
        self.server.server_close()
