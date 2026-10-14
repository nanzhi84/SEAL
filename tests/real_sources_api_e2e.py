"""Read-only live 19-source HTTP acceptance; uv run --no-sync python tests/real_sources_api_e2e.py."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import boto3
import psycopg
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
COMMAND = "uv run --no-sync python tests/real_sources_api_e2e.py"


def digest(b):
    return hashlib.sha256(b).hexdigest()


def write(p, v):
    p.write_text(json.dumps(v, ensure_ascii=False, indent=2, default=str) + "\n")


def main():
    name = "real-sources-api-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    work = ROOT / "work" / name
    art = ROOT / "artifacts" / name
    work.mkdir(mode=448)
    art.mkdir()
    env = dict(os.environ)
    for line in (ROOT / ".env.local").read_text().splitlines():
        if "=" in line and (not line.lstrip().startswith("#")):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("\"'")
    env["PGOPTIONS"] = "-c default_transaction_read_only=on"
    env["SEAL_WORK_DIR"] = str(work / "service")
    key = secrets.token_urlsafe(40)
    token_key = Fernet.generate_key().decode()
    private_values = [key, token_key] + [
        v
        for k, v in env.items()
        if any((x in k for x in ("PASSWORD", "SECRET", "ACCESS_KEY", "DATABASE_URL"))) and v
    ]
    source_start = {
        str(p.relative_to(ROOT)): digest(p.read_bytes())
        for p in sorted((ROOT / "src").rglob("*.py"))
    }
    redactions = {}
    checks = []
    entries = []
    proc = None
    log = None

    def clean(v):
        if isinstance(v, dict):
            return {k: clean(value) for k, value in v.items()}
        if isinstance(v, (list, tuple)):
            return [clean(value) for value in v]
        if not isinstance(v, str):
            return v
        text = v
        for secret in private_values:
            text = text.replace(secret, "[REDACTED_CREDENTIAL]")
        for label, pattern in [
            ("token", "gAAAA[A-Za-z0-9_=-]+"),
            ("email", "[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}"),
            ("mobile", "(?<!\\d)1[3-9]\\d{9}(?!\\d)"),
            ("identity", "(?<!\\d)\\d{17}[\\dXx](?!\\d)"),
        ]:
            text, n = re.subn(pattern, "[REDACTED_" + label.upper() + "]", text)
            redactions[label] = redactions.get(label, 0) + n
        return text

    def save(p, v):
        write(p, clean(v))

    def check(label, ok, **evidence):
        checks.append(dict(assertion=label, status="PASS" if ok else "FAIL", **evidence))
        save(art / "assertions.json", checks)

    def query(q, args=()):
        with psycopg.connect(
            env["SEAL_DATABASE_URL"], options=env["PGOPTIONS"], row_factory=dict_row
        ) as conn:
            return conn.execute(q, args).fetchall()

    def snapshot():
        out = {}
        with psycopg.connect(
            env["SEAL_DATABASE_URL"], options=env["PGOPTIONS"], row_factory=dict_row
        ) as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            for row in c.execute(
                "SELECT table_schema,table_name FROM information_schema.tables WHERE table_schema IN ('seal','public') AND table_type='BASE TABLE' ORDER BY 1,2"
            ).fetchall():
                table = row["table_schema"] + "." + row["table_name"]
                rows = c.execute(
                    sql.SQL("SELECT row_to_json(t) AS r FROM {} t").format(
                        sql.Identifier(row["table_schema"], row["table_name"])
                    )
                ).fetchall()
                encoded = sorted((json.dumps(r["r"], sort_keys=True, default=str) for r in rows))
                out[table] = dict(rows=len(rows), sha256=digest("\n".join(encoded).encode()))
        objects = sorted(
            (
                (r["Key"], r["Size"], r["ETag"])
                for page in s3.get_paginator("list_objects_v2").paginate(
                    Bucket=env["SEAL_S3_BUCKET"]
                )
                for r in page.get("Contents", [])
            )
        )
        out["s3"] = dict(objects=len(objects), sha256=digest(json.dumps(objects).encode()))
        return out

    def request(url, file):
        if urlsplit(url).netloc != urlsplit(base).netloc:
            raise RuntimeError("non-loopback Link rejected")
        start = time.monotonic()
        try:
            response = urlopen(Request(url, headers={"Authorization": "Bearer " + key}), timeout=70)
        except HTTPError as e:
            response = e
        with response:
            raw = response.read()
            status = response.status
            headers = dict(response.headers)
        body = json.loads(raw)
        save(art / file, body)
        return (
            status,
            headers,
            body,
            dict(
                status=status,
                bytes=len(raw),
                raw_sha256=digest(raw),
                response=file,
                elapsed_seconds=round(time.monotonic() - start, 3),
                cache_control=headers.get("Cache-Control"),
            ),
        )

    ids = [
        "dd-006",
        "dd-017",
        "dd-033",
        "dd-040",
        "dd-041",
        "dd-050",
        "dd-051",
        "dd-061",
        "dd-066",
        "dd-102",
        "dd-155",
        "dd-176",
        "dd-183",
        "dd-219",
        "dd-231",
        "dd-233",
        "dd-328",
        "dd-448",
        "dd-449",
    ]
    s3 = boto3.client(
        "s3",
        endpoint_url=env["SEAL_S3_ENDPOINT"],
        aws_access_key_id=env["SEAL_S3_ACCESS_KEY"],
        aws_secret_access_key=env["SEAL_S3_SECRET_KEY"],
        region_name=env.get("SEAL_S3_REGION", "us-east-1"),
    )
    try:
        write(
            art / "failure-boundaries.json",
            json.loads((ROOT / "tests/real_sources_api_failure_boundaries.json").read_text()),
        )
        check("exactly 19 authorized IDs", len(ids) == 19)
        readonly = query("SHOW default_transaction_read_only")[0]["default_transaction_read_only"]
        check("connections default read-only", readonly == "on")
        roles = query("SELECT rolname FROM pg_roles WHERE rolcanlogin")
        registered = query(
            "SELECT id,config->'entry_urls' AS entry_urls FROM seal.source WHERE id=ANY(%s) ORDER BY id COLLATE \"C\"",
            (ids,),
        )
        expected = {r["id"]: list(dict.fromkeys(r["entry_urls"])) for r in registered}
        current = query(
            'SELECT f.source_id,r.id,r.url,v.id AS version_id,v.result_ref,v.cursor FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id JOIN seal.version v ON v.id=r.current_version_id WHERE f.source_id=ANY(%s) ORDER BY f.source_id,r.id COLLATE "C"',
            (ids,),
        )
        save(art / "current-success-sql.json", current)
        before = snapshot()
        save(art / "readonly-before.json", before)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        cfg = dict(
            public_base_url=base,
            token_key=token_key,
            callers=[dict(id="real-19-reader", key_sha256=digest(key.encode()), sources=ids)],
            workers=4,
            request_timeout_seconds=60,
            result_max_bytes=16777216,
            attachment_max_bytes=52428800,
            page_max_bytes=67108864,
        )
        private = work / "config.private.json"
        write(private, cfg)
        private.chmod(384)
        logpath = work / "service.private.log"
        log = logpath.open("w")
        logpath.chmod(384)
        argv = [
            sys.executable,
            "-m",
            "seal.cli",
            "serve",
            "--config",
            str(private),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(250):
            if proc.poll() is not None:
                raise RuntimeError("serve exited: " + str(clean(logpath.read_text())))
            try:
                with socket.create_connection(("127.0.0.1", port), 0.2):
                    break
            except OSError:
                time.sleep(0.1)
        status, headers, body, receipt = request(base + "/sources", "sources.actual.json")
        save(art / "sources.receipt.json", receipt)
        check(
            "sources response strict authorized 19 and SQL registered entries",
            status == 200
            and isinstance(body, list)
            and (body == [dict(id=k, entry_urls=v) for k, v in expected.items()])
            and (len(body) == 19),
            actual_status=status,
        )
        for ident, urls in expected.items():
            want = [r["url"] for r in current if r["source_id"] == ident]
            for entry_no, entry in enumerate(urls, 1):
                rec = dict(
                    source_id=ident,
                    entry_url=entry,
                    expected_items=len(want),
                    pages=[],
                    items=0,
                    attachments=0,
                )
                next_url = base + "/results?" + urlencode(dict(source_url=entry, limit=100))
                seen = []
                visited = set()
                while next_url:
                    if next_url in visited or len(visited) > 10000:
                        raise RuntimeError("pagination cycle or bound")
                    visited.add(next_url)
                    page_no = len(rec["pages"]) + 1
                    filename = f"{ident}-entry{entry_no}-page{page_no:03}.actual.json"
                    status, headers, body, receipt = request(next_url, filename)
                    rec["pages"].append(receipt)
                    if status != 200:
                        rec.update(status="FAIL", http_status=status, error=body)
                        print(ident, entry_no, "FAIL", status, body, flush=True)
                        break
                    if not isinstance(body, list):
                        rec.update(status="FAIL", error="not_array")
                        break
                    seen.extend((item["url"] for item in body))
                    rec["attachments"] += sum((len(item["attachments"]) for item in body))
                    check(
                        f"{ident} entry{entry_no} page{page_no} fields",
                        all(
                            (
                                set(item)
                                == {
                                    "url",
                                    "markdown",
                                    "metadata",
                                    "links",
                                    "media",
                                    "tables",
                                    "attachments",
                                }
                                for item in body
                            )
                        )
                        and headers.get("Cache-Control") == "no-store",
                    )
                    link = headers.get("Link")
                    match = re.fullmatch('<([^>]+)>; rel="next"', link) if link else None
                    if link and (not match):
                        raise RuntimeError("invalid Link format")
                    next_url = match.group(1) if match else None
                rec["items"] = len(seen)
                rec["actual_urls_sha256"] = digest(json.dumps(seen).encode())
                rec["duplicate_urls"] = len(seen) - len(set(seen))
                rec["missing_urls"] = sorted(set(want) - set(seen))
                rec["unexpected_urls"] = sorted(set(seen) - set(want))
                if "status" not in rec:
                    rec["status"] = (
                        "PASS" if seen == want and len(seen) == len(set(seen)) else "FAIL"
                    )
                entries.append(rec)
                save(art / "entries.json", entries)
                print(ident, entry_no, rec["status"], len(seen), "/", len(want), flush=True)
        source_end = {
            str(p.relative_to(ROOT)): digest(p.read_bytes())
            for p in sorted((ROOT / "src").rglob("*.py"))
        }
        check("product code unchanged during acceptance", source_start == source_end)
        after = snapshot()
        save(art / "readonly-after.json", after)
        drift = {k for k in before if before[k] != after[k]}
        check("SQL and S3 contents unchanged", not drift, changed_tables=sorted(drift))
        if drift:
            for rec in entries:
                if rec["status"] == "PASS":
                    rec["status"] = "UNKNOWN"
        save(
            art / "environment.json",
            dict(
                command=COMMAND,
                python=sys.version,
                base=base,
                authorized_ids=ids,
                registered_entries=sum(map(len, expected.values())),
                git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                source_sha256={
                    str(p.relative_to(ROOT)): digest(p.read_bytes())
                    for p in sorted((ROOT / "src").rglob("*.py"))
                },
                test_sha256=digest(Path(__file__).read_bytes()),
                database_mode="Existing seal login (not least-privilege); acceptance SQL connections default read-only verified. Service receives PGOPTIONS but enforcement is not independently proven; interface read-only transaction contract plus unchanged SQL/S3 receipts. No role mutations",
                existing_login_roles=[r["rolname"] for r in roles],
                s3_mode="Existing local archive credential; GET/LIST paths only; no writes",
                premises="Existing .env.local local PostgreSQL/MinIO and previously collected 19-source data required; uv runtime installed",
                scope="No collect/recheck/migrate/init; public websites not fetched; attachment bodies not downloaded",
                response_redaction="Actual complete JSON pages retained; ephemeral encrypted tokens, credential values and detected email/mobile/identity strings redacted",
            ),
        )
    except Exception as exc:
        save(art / "fatal.json", dict(error_type=type(exc).__name__, message=str(exc)))
        check("acceptance completes", False, error_type=type(exc).__name__)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if log:
            log.close()
        for p in work.glob("*.private.*"):
            p.unlink()
        save(art / "entries.json", entries)
        summary = dict(
            command=COMMAND,
            artifacts=str(art),
            sources=len(ids),
            entries=len(entries),
            passed_entries=sum((e["status"] == "PASS" for e in entries)),
            failed_entries=sum((e["status"] == "FAIL" for e in entries)),
            unknown_entries=sum((e["status"] == "UNKNOWN" for e in entries)),
            returned_items=sum((e["items"] for e in entries)),
            pages=sum((len(e["pages"]) for e in entries)),
            assertions_pass=sum((c["status"] == "PASS" for c in checks)),
            assertions_fail=sum((c["status"] == "FAIL" for c in checks)),
            redactions=redactions,
            service_stopped=True,
            ephemeral_configuration_removed=True,
            unverified=[
                "Production deployment",
                "External website availability",
                "Full-site collection completeness",
                "Attachment download",
            ],
        )
        save(art / "summary.json", summary)
        (art / "summary.md").write_text(
            "# 真实19源只读结果验收\n\n运行命令：`"
            + COMMAND
            + "`\n\n前提：现有 `.env.local`、本地 PostgreSQL/MinIO 与已完成采集数据；不写库、不迁移、不采集。响应中的个人联系信息和临时令牌已脱敏。\n\n"
            + "\n".join(
                (
                    f"- {e['source_id']}：{e['status']}；入口 {e['entry_url']}；{e['items']}/{e['expected_items']} 项，{len(e['pages'])} 页；错误 {e.get('error', '无')}"
                    for e in entries
                )
            )
            + "\n\n完整实际JSON及SHA见各page文件/entries.json；SQL和S3前后内容哈希见readonly文件。附件下载、生产部署和全站完整性未验证。\n"
        )
        scan = "\n".join((p.read_text() for p in art.iterdir() if p.is_file()))
        assert not any((secret in scan for secret in private_values)), "artifact credential leak"
        print("ARTIFACTS", art, flush=True)
    return int(
        any((c["status"] == "FAIL" for c in checks))
        or any((e["status"] != "PASS" for e in entries))
    )


if __name__ == "__main__":
    raise SystemExit(main())
