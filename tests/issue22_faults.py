"""Real SQL/S3 faults, bounded HTTP concurrency and read-only behavior acceptance."""
from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from issue22_contract import results
from issue22_lab import digest, write


class S3Proxy:
    """Actual HTTP proxy delays complete existing S3 bytes; no product module patching."""
    def __init__(self, endpoint, delay=7):
        self.endpoint = endpoint
        self.delay = delay
        self.started = threading.Event()
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()
        proxy = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_HEAD(self):
                with urlopen(Request(proxy.endpoint + self.path, headers=dict(self.headers), method="HEAD")) as response:
                    self.send_response(response.status)
                    for k, v in response.headers.items():
                        if k.lower() not in ("server", "date", "connection"):
                            self.send_header(k, v)
                    self.end_headers()
            def do_GET(self):
                with proxy.lock:
                    proxy.active += 1
                    proxy.maximum = max(proxy.maximum, proxy.active)
                proxy.started.set()
                try:
                    with urlopen(Request(proxy.endpoint + self.path, headers=dict(self.headers))) as response:
                        body = response.read()
                        headers = dict(response.headers)
                    self.send_response(200)
                    for k, v in headers.items():
                        if k.lower() not in ("server", "date", "connection"):
                            self.send_header(k, v)
                    self.end_headers()
                    # Deliver a first chunk, then stall real body read across API deadline.
                    self.wfile.write(body[:1])
                    self.wfile.flush()
                    time.sleep(proxy.delay)
                    self.wfile.write(body[1:])
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with proxy.lock:
                        proxy.active -= 1
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def archive_faults(lab, data):
    for source, code in (("dd-006", "archive_missing"), ("dd-007", "archive_corrupt"),
        ("dd-008", "archive_invalid"), ("dd-009", "archive_invalid"), ("dd-010", "archive_invalid")):
        lab.error(results(data, source), code, 500)
    # Vary valid SHA envelopes and types; whole source request must fail, never skip the bad row.
    cases = [
        ("JSON envelope wrong type", lambda e: [e]),
        ("download encoding missing", lambda e: {"result": e["result"], "attachment_refs": []}),
        ("result wrong type", lambda e: {**e, "result": []}),
        ("markdown raw wrong type", lambda e: change(e, "markdown", {"raw_markdown": 42})),
        ("metadata author object", lambda e: change(e, "metadata", {"author": {"value": "bad"}})),
        ("links href integer", lambda e: change(e, "links", {"internal": [{"href": 1}], "external": []})),
        ("media negative width", lambda e: change(e, "media", {"images": [{"src": "https://fixture.example.org/image", "width": -1}]})),
        ("media boolean width", lambda e: change(e, "media", {"images": [{"width": True}]})),
        ("media nonnumeric width", lambda e: change(e, "media", {"images": [{"width": "wide"}]})),
        ("table headers integer", lambda e: change(e, "tables", [{"headers": [7], "rows": [["content"]]}])),
        ("table rows scalar", lambda e: change(e, "tables", [{"headers": ["A"], "rows": ["bad"]}])),
        ("table caption object", lambda e: change(e, "tables", [{"headers": [], "rows": [], "caption": {}}])),
        ("attachment list wrong type", lambda e: {**e, "attachment_refs": "a" * 64})]
    for label, mutate in cases:
        envelope = mutate(data.envelope("bad boundary"))
        data.success("dd-015", "invalid-boundaries", envelope)
        before = len(lab.checks)
        lab.error(results(data, "dd-015"), "archive_invalid", 500)
        lab.check("strict typed boundary " + label, lab.checks[before]["status"] == "PASS")
    valid = data.envelope("Good typed boundary")
    valid["result"]["media"] = dict(images=[dict(src="https://fixture.example.org/image", width="0042", score=999, data="PRIVATE_MEDIA")], videos=[], audios=[])
    valid["result"]["tables"] = [dict(headers=["A"], rows=[["B"]], caption=None, summary="Retained summary", metadata={"private": True})]
    data.success("dd-015", "invalid-boundaries", valid)
    status, _, rows, _, _ = lab.request(results(data, "dd-015"))
    lab.check("numeric SDK width explicitly becomes nonnegative integer; media/table extra fields removed", status == 200
        and rows[0]["media"]["images"] == [dict(src="https://fixture.example.org/image", alt=None, desc=None, type=None, format=None, width=42)]
        and rows[0]["tables"] == [dict(headers=["A"], rows=[["B"]], caption=None, summary="Retained summary")])
    token = data.old_attachment_token
    url = lab.base + "/attachments/" + token
    original = data.attachment_bytes
    lab.s3.delete_object(Bucket=lab.bucket, Key=data.attachment_key)
    lab.error(url, "archive_missing", 500)
    lab.s3.put_object(Bucket=lab.bucket, Key=data.attachment_key, Body=b"damaged original bytes")
    lab.error(url, "archive_corrupt", 500)
    lab.s3.put_object(Bucket=lab.bucket, Key=data.attachment_key, Body=original)


def change(envelope, field, value):
    altered = copy.deepcopy(envelope)
    altered["result"][field] = value
    return altered


def limits(lab, data):
    lab.start_service(result_max_bytes=1024)
    lab.error(results(data, "dd-014"), "archive_too_large", 500)
    lab.start_service(attachment_max_bytes=1024)
    lab.error(lab.base + "/attachments/" + data.old_attachment_token, "archive_too_large", 500)
    lab.start_service(page_max_bytes=1024)
    lab.error(results(data, "dd-002", limit=100), "archive_too_large", 500)


def dependencies(lab, data):
    lab.start_service()
    lab.docker("stop", "--time", "1", lab.containers[0])
    try:
        st, _, value, _, elapsed = lab.request(results(data, "dd-001"))
        lab.check("real stopped PostgreSQL yields database_unavailable503 within bounded deadline", st == 503
                  and value == {"error": "database_unavailable"} and elapsed < 7, elapsed=elapsed)
        lab.error("/results?bad", "unauthenticated", 401, key=None)
    finally:
        lab.docker("start", lab.containers[0])
        ready(lab, "db")
    lab.docker("stop", "--time", "1", lab.containers[1])
    try:
        st, _, value, _, elapsed = lab.request(results(data, "dd-001"))
        lab.check("real stopped MinIO yields object_storage_unavailable503 within bounded deadline", st == 503
                  and value == {"error": "object_storage_unavailable"} and elapsed < 7, elapsed=elapsed)
        lab.error(results(data, "dd-012"), "source_forbidden", 403, key=1)
    finally:
        lab.docker("start", lab.containers[1])
        ready(lab, "s3")
    st, _, value, _, _ = lab.request(results(data, "dd-001"))
    lab.check("dependency recovery restores complete successful array", st == 200 and isinstance(value, list) and len(value) == 3)


def ready(lab, target):
    until = time.monotonic() + 35
    while True:
        try:
            lab.sql("SELECT 1") if target == "db" else lab.s3.list_buckets()
            return
        except Exception:
            if time.monotonic() > until:
                raise
            time.sleep(.2)


def timeout_and_busy(lab, data):
    # Forward genuine signed HTTP requests without logging headers; HEAD remains immediate,
    # only GetObject body delivery crosses the deliberately slow real transport.
    proxy = S3Proxy(lab.endpoint, delay=7)
    proxy.start()
    try:
        lab.start_service(workers=1, request_timeout_seconds=1,
                          environment={"SEAL_S3_ENDPOINT": proxy.base})
        path = results(data, "dd-001")
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(lab.request, path)
            lab.check("slow real S3 response reached backend", proxy.started.wait(5))
            siblings = [pool.submit(lab.request, path) for _ in range(3)]
            first_response = first.result(timeout=15)
            responses = [f.result(timeout=15) for f in siblings]
        lab.check("slow body yields stable request_timeout503 within bounded HTTPdeadline", first_response[0] == 503
                  and first_response[2] == {"error": "request_timeout"} and first_response[4] < 3,
                  elapsed=first_response[4])
        lab.check("timed-out worker retains slot and siblings receive bounded server_busy", all(r[0] == 503
            and r[2] == {"error": "server_busy"} and r[4] < 3 for r in responses),
            sibling_statuses=[dict(status=r[0], body=r[2], elapsed=r[4]) for r in responses])
        lab.check("workers1 does not grow S3 work under concurrent timeouts", proxy.maximum == 1, maximum=proxy.maximum)
        time.sleep(7)
        st, _, empty, _, elapsed = lab.request(results(data, "dd-003"))
        lab.check("worker slot released when timed-out backend completes", st == 200 and empty == [] and elapsed < 2)
    finally:
        lab.stop_service()
        proxy.close()


def readonly(lab, data):
    before = lab.snapshot()
    requests_before = len(lab.origin.requests)
    work_before = {str(p.relative_to(lab.work)): digest(p.read_bytes()) for p in (lab.work / "seal").rglob("*") if p.is_file()}
    lab.start_service()
    for source in ("dd-001", "dd-002", "dd-003", "dd-011", "dd-012", "dd-015"):
        lab.request(results(data, source))
    lab.request("/attachments/" + data.old_attachment_token)
    lab.request(results(data, "dd-012"), key=1)
    after = lab.snapshot()
    work_after = {str(p.relative_to(lab.work)): digest(p.read_bytes()) for p in (lab.work / "seal").rglob("*") if p.is_file()}
    lab.check("serve start/query/attachment/error leaves every DB table and S3 object unchanged", before == after)
    lab.check("results source lookup never requests site or starts collection", requests_before == len(lab.origin.requests))
    lab.check("results service creates no disk archive materialization", work_before == work_after)
    write(lab.artifacts / "readonly-snapshots.json", dict(before=before, after=after,
        origin_requests_before=requests_before, origin_requests_after=len(lab.origin.requests),
        disk_files_before=work_before, disk_files_after=work_after))
