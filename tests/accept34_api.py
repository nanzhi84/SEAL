"""API-only acceptance client: frozen 16-column records and CAS byte reconciliation."""

import contextlib
import hashlib
import json
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from cryptography.fernet import Fernet

API_HEADERS = [
    "资料名称",
    "解析后文件",
    "资料类型",
    "采集时间",
    "原始文件",
    "文件顺序",
    "文号/案号",
    "发布机构",
    "发布日期",
    "生效日期",
    "失效日期",
    "状态",
    "来源名称",
    "来源详情页",
    "来源记录号",
]
API_COLUMNS = [
    "title",
    "markdown_file",
    "document_type",
    "acquired_at",
    "original_file",
    "volume_order",
    "document_number",
    "issuer",
    "published_at",
    "effective_at",
    "expired_at",
    "status",
    "source_name",
    "detail_url",
    "source_record_hash",
]


def s3client(t):
    return boto3.client(
        "s3",
        endpoint_url=t.ENV["SEAL_S3_ENDPOINT"],
        aws_access_key_id=t.ENV["SEAL_S3_ACCESS_KEY"],
        aws_secret_access_key=t.ENV["SEAL_S3_SECRET_KEY"],
        region_name=t.ENV.get("SEAL_S3_REGION", "us-east-1"),
    )


@contextlib.contextmanager
def api_session(t, label):
    token = secrets.token_urlsafe(24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    cfg = {
        "public_base_url": base,
        "token_key": Fernet.generate_key().decode(),
        "file_max_bytes": 134217728,
        "request_timeout_seconds": 30,
        "callers": [
            {
                "id": "accept34",
                "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "sources": [t.SOURCE],
            }
        ],
    }
    path = t.WORK / (label + "-api.json")
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    log = (t.ART / (label + "-api.log")).open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "seal",
            "serve",
            "--config",
            str(path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=t.ROOT,
        env=t.ENV,
        stdout=log,
        stderr=log,
    )
    t.PROCESSES.append(proc)
    receipts = []

    def get(route):
        request = urllib.request.Request(base + route, headers={"Authorization": "Bearer " + token})
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            headers = dict(response.headers)
            status = response.status
        receipts.append(
            {
                "route": route,
                "status": status,
                "headers": headers,
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
            }
        )
        t.save(label + "-http-receipts.json", receipts)
        return status, body, headers

    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=0.2)
                break
            except Exception:
                time.sleep(0.1)
        yield get
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(10)
        log.close()


def normalized(value):
    if value is None:
        return ""
    if hasattr(value, "strftime") and hasattr(value, "hour"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def api(t, sid, label="api", after_first_page=None, check_files=True):
    items = []
    page = 0
    with api_session(t, label) as get:
        route = "/results?source_id=" + t.SOURCE + "&limit=2"
        while route:
            status, body, headers = get(route)
            t.check(
                label + " results HTTP200",
                status == 200,
                {"status": status, "body": body.decode(errors="replace")[:300]},
            )
            data = json.loads(body)
            t.check(label + " API fixed snapshot", data.get("snapshot_id") == sid)
            items += data["items"]
            cursor = data.get("next_cursor")
            route = (
                "/results?source_id=" + t.SOURCE + "&limit=2&cursor=" + urllib.parse.quote(cursor)
                if cursor
                else None
            )
            page += 1
            if page == 1 and after_first_page:
                after_first_page()
        t.save(label + "-results.json", items)
        expected = t.query(
            "SELECT d.*,i.resource_id,i.source_id FROM seal.snapshot_item i JOIN seal.delivery_row d ON d.version_id=i.version_id WHERE i.snapshot_id=%s AND i.source_id=%s ORDER BY i.resource_id,d.row_number",
            (sid, t.SOURCE),
        )
        t.check(
            label + " all API rows have exact 16 business keys",
            all(
                {k for k in row if any(ord(c) > 127 for c in k)} == set(API_HEADERS)
                for row in items
            ),
            {"items": len(items), "expected_keys": API_HEADERS},
        )

        def canonical(row):
            return tuple(normalized(row.get(k)) for k in API_HEADERS) + (
                row.get("original_hash"),
                row.get("markdown_hash"),
            )

        expected_rows = [
            {
                **dict(zip(API_HEADERS, [row[k] for k in API_COLUMNS], strict=True)),
                "resource_id": row["resource_id"],
                "version_id": row["version_id"],
                "row_number": row["row_number"],
                "original_hash": row["original_hash"],
                "markdown_hash": row["markdown_hash"],
            }
            for row in expected
        ]
        t.check(
            label + " API rows match frozen Snapshot 16-column projection",
            sorted(map(canonical, items)) == sorted(map(canonical, expected_rows)),
            {"api_count": len(items), "snapshot_rows": len(expected_rows)},
        )
        t.check(
            label + " API resource Version row references match Snapshot",
            sorted((r["resource_id"], r["version_id"], r["row_number"]) for r in items)
            == sorted((r["resource_id"], r["version_id"], r["row_number"]) for r in expected_rows),
        )
        file_proof = {}
        if check_files:
            closure = t.query(
                "SELECT sha256,byte_size,media_type FROM seal.snapshot_file WHERE snapshot_id=%s AND source_id=%s ORDER BY sha256",
                (sid, t.SOURCE),
            )
            hashes = {r["original_hash"] for r in items if r["original_hash"]} | {
                r["markdown_hash"] for r in items
            }
            t.check(
                label + " API records cover every authorized Snapshot file",
                hashes == {r["sha256"] for r in closure},
            )
            folder = t.ART / (label + "-files")
            folder.mkdir(exist_ok=True)
            s3 = s3client(t)
            for row in closure:
                sha = row["sha256"]
                status, body, headers = get("/files/" + sha)
                digest = hashlib.sha256(body).hexdigest()
                lower_headers = {k.lower(): v for k, v in headers.items()}
                t.check(
                    label + " file bytes/hash " + sha[:12],
                    status == 200
                    and digest == sha
                    and body
                    == s3.get_object(Bucket=t.ENV["SEAL_S3_BUCKET"], Key=sha)["Body"].read(),
                )
                t.check(
                    label + " file response metadata " + sha[:12],
                    lower_headers.get("etag") == '"' + sha + '"'
                    and int(lower_headers.get("content-length", "-1"))
                    == len(body)
                    == row["byte_size"]
                    and lower_headers.get("content-type", "").startswith(row["media_type"]),
                    headers,
                )
                (folder / sha).write_bytes(body)
                file_proof[sha] = digest
    proof = {"snapshot_id": sid, "items": items, "files": file_proof}
    t.save(label + "-proof.json", proof)
    return proof
