"""Issue #31 independent acceptance support: isolated services, fixtures, evidence.

Nothing here imports ``seal``; the product is exercised only through its real CLI
(subprocess), its PostgreSQL ``seal`` schema, the S3 API and its HTTP API.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import psycopg
import requests
import yaml
from psycopg.rows import dict_row

from issue31_primitives import free_port

# --------------------------------------------------------------------------- HTTP fixture


class Fixture:
    """Controllable HTTP source on a fixed loopback port (identity includes URL)."""

    def __init__(self, port: int | None = None):
        self.routes: dict[str, tuple] = {}
        self.requests: list[str] = []
        self.hang: dict[str, threading.Event] = {}
        self.hits: dict[str, int] = {}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                path = self.path
                fixture.requests.append(path)
                fixture.hits[path] = fixture.hits.get(path, 0) + 1
                if path in fixture.hang:
                    fixture.hang[path].wait(600)
                route = fixture.routes.get(path)
                if isinstance(route, list):  # sequential responses; last one repeats
                    route = route.pop(0) if len(route) > 1 else route[0]
                if route is None:
                    route = (404, "text/plain", b"not found")
                status, media, body = route[:3]
                extra = route[3] if len(route) > 3 else {}
                if status == "reset":
                    # Promise more bytes than sent, then drop the connection.
                    self.send_response(200)
                    self.send_header("Content-Type", media)
                    self.send_header("Content-Length", str(len(body) + 50000))
                    self.end_headers()
                    with contextlib.suppress(Exception):
                        self.wfile.write(body[: max(1, len(body) // 3)])
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                self.send_response(status)
                self.send_header("Content-Type", media)
                self.send_header("Content-Length", str(len(body)))
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)

            def do_HEAD(self):  # noqa: N802
                route = self.routes_get()
                self.send_response(route[0] if isinstance(route[0], int) else 200)
                self.end_headers()

            def routes_get(self):
                return fixture.routes.get(self.path, (404, "text/plain", b""))

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port or 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def put(self, path, body: bytes | str, media="text/html; charset=utf-8", status=200, headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.routes[path] = (status, media, body, headers or {})
        return body

    def robots(self, text="User-agent: *\nAllow: /\n"):
        self.put("/robots.txt", text, "text/plain")

    def close(self):
        for event in self.hang.values():
            event.set()
        self.server.shutdown()
        self.server.server_close()


def html_page(title, body_html="", links=(), h1=True, extra_head=""):
    anchors = "".join(f'<p><a href="{href}">{text}</a></p>' for href, text in links)
    heading = f"<h1>{title}</h1>" if h1 else ""
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>{extra_head}</head>"
        f"<body><main>{heading}{body_html}{anchors}</main></body></html>"
    )


# --------------------------------------------------------------------------- br-ocr-v1 mock


class BrOcrMock:
    """Controllable /file_parse -> /status/{uuid} -> /result/{uuid} service."""

    def __init__(self):
        self.calls: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.result_factory = None  # callable(request_json) -> data dict
        self.status_polls_before_success = 1
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, payload, status=200):
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self):
                path = urlsplit(self.path).path
                for prefix in ("/api/v1",):
                    if path.startswith(prefix):
                        path = path[len(prefix):]
                return path

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path = self._route()
                ctype = self.headers.get("Content-Type", "")
                mock.calls.append({"method": "POST", "path": self.path, "content_type": ctype, "bytes": len(raw)})
                if path != "/file_parse":
                    return self._send({"code": 404, "msg": "no route"}, 404)
                request = {}
                if "json" in ctype:
                    with contextlib.suppress(Exception):
                        request = json.loads(raw)
                task = str(uuid.uuid4())
                data = mock.result_factory(request, raw) if mock.result_factory else {}
                mock.tasks[task] = {"polls": 0, "data": data}
                self._send({"code": 200, "data": {"task_uuid": task, "task_id": task, "uuid": task}})

            def do_GET(self):  # noqa: N802
                path = self._route()
                mock.calls.append({"method": "GET", "path": self.path})
                parts = path.strip("/").split("/")
                if len(parts) == 2 and parts[0] == "status" and parts[1] in mock.tasks:
                    task = mock.tasks[parts[1]]
                    task["polls"] += 1
                    state = "SUCCESS" if task["polls"] >= mock.status_polls_before_success else "RUNNING"
                    return self._send({"code": 200, "data": {"task_status": state, "status": state}})
                if len(parts) == 2 and parts[0] == "result" and parts[1] in mock.tasks:
                    return self._send({"code": 200, "data": mock.tasks[parts[1]]["data"]})
                return self._send({"code": 404, "msg": "unknown"}, 404)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_port}/api/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def br_result(pages: list[list[dict]]):
    """pages: list of block lists (0-based page_idx assigned)."""
    blocks, page_md = [], []
    for idx, page in enumerate(pages):
        texts = []
        for block in page:
            item = {"page_idx": idx, "type": block.get("type", "text"), "bbox": block.get("bbox", [50, 50 + 40 * len(texts), 550, 80 + 40 * len(texts)])}
            if item["type"] == "table":
                item["table_body"] = block["table_body"]
                texts.append(block["table_body"])
            else:
                item["text"] = block["text"]
                texts.append(block["text"])
            blocks.append(item)
        page_md.append("\n\n".join(texts))
    return {
        "whole_text_md": "\n\n".join(page_md),
        "page_content_json": json.dumps(page_md, ensure_ascii=False),
        "page_parse_json": json.dumps(blocks, ensure_ascii=False),
    }


