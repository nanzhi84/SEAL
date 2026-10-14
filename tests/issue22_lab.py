"""Independent Issue22 services/HTTP receipts; never imports result-service implementation."""
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
from urllib.request import Request, urlopen

import boto3
import psycopg
from botocore.config import Config
from cryptography.fernet import Fernet
from issue20_fixture import Origin
from issue20_lab import MC, MINIO
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


class Lab:
    def __init__(self):
        self.name = "issue22-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:5]
        self.work, self.artifacts = (ROOT / x / self.name for x in ("work", "artifacts"))
        self.work.mkdir(parents=True)
        self.artifacts.mkdir(parents=True)
        self.secrets = [secrets.token_hex(32) for _ in range(7)]
        self.token_key = Fernet.generate_key().decode()
        self.secrets.append(self.token_key)
        self.keys = [secrets.token_urlsafe(40) for _ in range(3)]
        self.secrets.extend(self.keys)
        self.containers, self.checks, self.commands, self.http_receipts = [], [], [], []
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SEAL_")}
        self.bucket = "issue22-archive"
        self.origin = Origin(self.work)
        self.service = None
        self.service_number = 0

    def clean(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "REDACTED")
        text = re.sub(r"gAAAA[A-Za-z0-9_=-]+", "REDACTED_TOKEN", text)
        return text

    def check(self, label, condition, **evidence):
        record = dict(assertion=label, status="PASS" if condition else "FAIL", **evidence)
        self.checks.append(record)
        print(record["status"], label, flush=True)
        write(self.artifacts / "assertions.json", self.checks)

    def docker(self, *args):
        return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.PIPE).strip()

    def sql(self, query, params=(), dsn=None):
        with psycopg.connect(dsn or self.dsn, autocommit=True, row_factory=dict_row) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else []

    def start(self):
        pg, minio = self.name + "-pg", self.name + "-minio"
        self.containers.extend([pg, minio])
        self.docker("run", "-d", "--name", pg, "-p", f"127.0.0.1:{free_port()}:5432", "-e",
                    "POSTGRES_PASSWORD=" + self.secrets[0], "-e", "POSTGRES_DB=issue22",
                    "-v", f'{self.work / "postgres"}:/var/lib/postgresql/data', "postgres:17-alpine")
        pg_port = self.docker("port", pg, "5432").rsplit(":", 1)[1]
        self.dsn = f"postgresql://postgres:{self.secrets[0]}@127.0.0.1:{pg_port}/issue22"
        self.read_dsn = f"postgresql://api_reader:{self.secrets[5]}@127.0.0.1:{pg_port}/issue22"
        self.docker("run", "-d", "--name", minio, "-p", f"127.0.0.1:{free_port()}:9000", "-e",
                    "MINIO_ROOT_USER=" + self.secrets[1], "-e", "MINIO_ROOT_PASSWORD=" + self.secrets[2],
                    "-v", f'{self.work / "minio"}:/data', MINIO, "server", "/data")
        minio_port = self.docker("port", minio, "9000").rsplit(":", 1)[1]
        self.endpoint = f"http://127.0.0.1:{minio_port}"
        self.s3 = boto3.client("s3", endpoint_url=self.endpoint, aws_access_key_id=self.secrets[1],
            aws_secret_access_key=self.secrets[2], region_name="us-east-1",
            config=Config(retries={"max_attempts": 0}, connect_timeout=2, read_timeout=3,
                          s3={"addressing_style": "path"}))
        deadline = time.monotonic() + 45
        while True:
            try:
                self.sql("SELECT 1")
                self.s3.list_buckets()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.2)
        self.s3.create_bucket(Bucket=self.bucket)
        self.admin_env = {**self.env, "SEAL_DATABASE_URL": self.dsn,
            "SEAL_S3_ENDPOINT": self.endpoint, "SEAL_S3_BUCKET": self.bucket,
            "SEAL_S3_ACCESS_KEY": self.secrets[1], "SEAL_S3_SECRET_KEY": self.secrets[2],
            "SEAL_WORK_DIR": str(self.work / "seal"), "SEAL_ALLOW_LOOPBACK": "1", "NO_COLOR": "1"}
        self.cli("init")
        self.sql(sql.SQL("CREATE ROLE api_reader LOGIN PASSWORD {}").format(sql.Literal(self.secrets[5])))
        self.sql("GRANT CONNECT ON DATABASE issue22 TO api_reader")
        self.sql("GRANT USAGE ON SCHEMA seal TO api_reader")
        self.sql("GRANT SELECT ON ALL TABLES IN SCHEMA seal TO api_reader")
        self.sql("ALTER ROLE api_reader SET default_transaction_read_only=on")
        policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::" + self.bucket]},
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::" + self.bucket + "/*"]}]}
        write(self.work / "policy.json", policy)
        command = ('mc alias set lab http://127.0.0.1:9000 "$ROOT_ID" "$ROOT_SECRET" >/dev/null && '
                   'mc admin user add lab "$APP_ID" "$APP_SECRET" >/dev/null && '
                   'mc admin policy create lab readonly /input/policy.json >/dev/null && '
                   'mc admin policy attach lab readonly --user "$APP_ID" >/dev/null')
        self.docker("run", "--rm", "--network", "container:" + minio, "-v", f"{self.work}:/input:ro",
                    "-e", "ROOT_ID=" + self.secrets[1], "-e", "ROOT_SECRET=" + self.secrets[2],
                    "-e", "APP_ID=" + self.secrets[3], "-e", "APP_SECRET=" + self.secrets[4],
                    "--entrypoint", "/bin/sh", MC, "-c", command)
        self.env = {**self.admin_env, "SEAL_DATABASE_URL": self.read_dsn,
            "SEAL_S3_ACCESS_KEY": self.secrets[3], "SEAL_S3_SECRET_KEY": self.secrets[4]}
        self.origin.start()
        write(self.artifacts / "environment.json", dict(
            command="uv run --no-sync python tests/issue22_e2e.py", python=sys.version,
            postgres_image="postgres:17-alpine", minio_image=MINIO,
            credentials="Fresh random DB/root/read-only credentials; omitted from artifacts",
            permissions="Service DB SELECT/default read-only; S3 Get/List; collector uses isolated admin",
            isolation="Fresh containers and loopback ports; work data only; root minio never accessed",
            git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            source_sha256={str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sorted((ROOT / "src").rglob("*.py"))},
            test_sha256={p.name: digest(p.read_bytes()) for p in sorted((ROOT / "tests").glob("issue22*.py"))},
            stable_failure_boundaries_sha256=digest((ROOT / "tests/issue22_failure_boundaries.json").read_bytes())))
        (self.artifacts / "public-contract.md").write_bytes((ROOT / "docs/plans/results-api.md").read_bytes())

    def cli(self, *args, expected=(0,), timeout=180):
        start = time.monotonic()
        cmd = [sys.executable, "-m", "seal.cli", *map(str, args)]
        p = subprocess.run(cmd, cwd=ROOT, env=self.admin_env, capture_output=True, text=True, timeout=timeout)
        self.commands.append(dict(argv=cmd, exit=p.returncode, elapsed=time.monotonic() - start,
                                 stdout=self.clean(p.stdout), stderr=self.clean(p.stderr)))
        self.check("fixture CLI " + args[0] + " accepted exit", p.returncode in expected, actual=p.returncode)
        write(self.artifacts / "commands.json", self.commands)
        return p

    def start_service(self, **overrides):
        self.stop_service()
        self.service_number += 1
        self.base = f"http://127.0.0.1:{free_port()}"
        cfg = dict(public_base_url=self.base, token_key=self.token_key,
            callers=[dict(id="all", key_sha256=digest(self.keys[0].encode()), sources=self.allowed),
                     dict(id="limited", key_sha256=digest(self.keys[1].encode()), sources=["dd-001"]),
                     dict(id="alternate", key_sha256=digest(self.keys[2].encode()), sources=["dd-001"])],
            workers=2, request_timeout_seconds=3, result_max_bytes=16777216,
            attachment_max_bytes=52428800, page_max_bytes=67108864)
        via_environment = overrides.pop("via_environment", False)
        env = dict(self.env)
        env.update(overrides.pop("environment", {}))
        cfg.update(overrides)
        path = self.work / f"api-{self.service_number}.private.json"
        write(path, cfg)
        path.chmod(0o600)
        self.service_config = cfg
        self.service_log = self.work / f"service-{self.service_number}.private.log"
        self.service_log_file = self.service_log.open("w")
        argv = [sys.executable, "-m", "seal.cli", "serve"]
        if via_environment:
            env["SEAL_API_CONFIG"] = str(path)
        else:
            argv.extend(["--config", str(path)])
        argv.extend(["--host", "127.0.0.1", "--port", self.base.rsplit(":", 1)[1]])
        self.service = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=self.service_log_file,
                                        stderr=subprocess.STDOUT)
        self.commands.append(dict(argv=argv, config="Private JSON omitted; randomized digest keys", kind="serve"))
        deadline = time.monotonic() + 25
        while True:
            if self.service.poll() is not None:
                raise RuntimeError("serve process exited; " + self.clean(self.service_log.read_text()))
            try:
                with socket.create_connection(("127.0.0.1", int(self.base.rsplit(":", 1)[1])), timeout=.2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("serve did not listen within25seconds") from None
                time.sleep(.1)

    def stop_service(self):
        if self.service is None:
            return
        self.service.terminate()
        try:
            self.service.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.service.kill()
            self.service.wait()
        self.service_log_file.close()
        raw_log = self.service_log.read_text()
        self.check("service protocol log omits Key/token/source URL and stack traces",
            not any(secret in raw_log for secret in self.secrets)
            and not re.search(r"gAAAA[A-Za-z0-9_=-]+", raw_log)
            and not any(url in raw_log for url in getattr(self, "source_entries", {}).values())
            and "Traceback" not in raw_log)
        (self.artifacts / f"service-{self.service_number}.log").write_text(self.clean(raw_log))
        self.service = None

    def request(self, path, key=0, headers=None, method="GET", timeout=15):
        if not path.startswith("http"):
            path = self.base + path
        request_headers = {} if key is None else {"Authorization": "Bearer " + self.keys[key]}
        request_headers.update(headers or {})
        start = time.monotonic()
        try:
            response = urlopen(Request(path, headers=request_headers, method=method), timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            status = response.status
            rh = dict(response.headers)
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            value = None
        receipt = dict(url=path, method=method, caller=key, status=status,
            headers=rh, body=value if value is not None else dict(bytes=len(body), sha256=digest(body)),
            elapsed=time.monotonic() - start)
        self.http_receipts.append(json.loads(self.clean(json.dumps(receipt, ensure_ascii=False))))
        write(self.artifacts / "http.json", self.http_receipts)
        return status, rh, value, body, receipt["elapsed"]

    def error(self, path, code, status, **options):
        actual, headers, value, _, elapsed = self.request(path, **options)
        self.check(f"{code} returns {status} safe error", actual == status and value == {"error": code},
                   actual_status=actual, body=value, elapsed=elapsed)
        return headers

    def body(self, key):
        stream = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            return stream.read()
        finally:
            stream.close()

    def put(self, raw):
        key = digest(raw)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=raw)
        return key

    def snapshot(self):
        tables = self.sql("SELECT table_schema,table_name FROM information_schema.tables "
            "WHERE table_type='BASE TABLE' AND table_schema IN ('seal','public') ORDER BY1,2".replace("BY1", "BY 1"))
        output = {}
        for item in tables:
            name = item["table_schema"] + "." + item["table_name"]
            rows = self.sql(sql.SQL("SELECT row_to_json(t) AS r FROM {} t").format(
                sql.Identifier(item["table_schema"], item["table_name"])))
            output[name] = digest(json.dumps(sorted([r["r"] for r in rows], key=lambda r: json.dumps(r, sort_keys=True)), sort_keys=True).encode())
        output["s3"] = sorted((r["Key"], r["Size"], r["ETag"]) for page in self.s3.get_paginator(
            "list_objects_v2").paginate(Bucket=self.bucket) for r in page.get("Contents", []))
        return output

    def close(self):
        self.stop_service()
        self.origin.close()
        for name in reversed(self.containers):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        write(self.artifacts / "commands.json", self.commands)
        write(self.artifacts / "summary.json", dict(
            passed=sum(c["status"] == "PASS" for c in self.checks),
            failed=sum(c["status"] == "FAIL" for c in self.checks),
            command="uv run --no-sync python tests/issue22_e2e.py", artifacts=str(self.artifacts),
            work=str(self.work), unverified=["Production deployment/HTTPS proxy/public-web availability", "One hour real wallclock waiting; authentic reissued old tokens cover expiry"] ))
        print("ARTIFACTS", self.artifacts, flush=True)
