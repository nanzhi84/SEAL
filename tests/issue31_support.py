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

from issue31_primitives import ROOT, MINIO_IMAGE, PG_IMAGE, NotVerified, free_port, sha256, template_headers
from issue31_http import BrOcrMock, Fixture, br_result, html_page
from issue31_builders import (PdfBuilder, docx_bytes, noise_page_jpeg, plain_rtf, scanned_page_jpeg,
                              soffice_convert, tcvn3_rtf, xlsx_bytes, zip_bytes)

# --------------------------------------------------------------------------- environment / evidence

SECRET_PATTERNS = [re.compile(r"postgresql://[^\s\"']+"), re.compile(r"Bearer [A-Za-z0-9_\-]+")]


class Lab:
    def __init__(self, name="issue31"):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.name = f"{name}-{stamp}"
        self.work = ROOT / "work" / self.name
        self.artifacts = ROOT / "artifacts" / self.name
        self.work.mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.env = {k: v for k, v in os.environ.items() if not (k.startswith("SEAL_") or k.startswith("AWS_"))}
        self.env.update(SEAL_ALLOW_LOOPBACK="1", SEAL_WORK_DIR=str(self.work / "runtime"), PYTHONUNBUFFERED="1")
        self.containers: list[str] = []
        self.secrets: list[str] = []
        self.commands: list[dict] = []
        self.results: list[dict] = []
        self.assertions: list[dict] = []
        self.current_test = None
        self.s3 = None
        self.bucket = "issue31-" + secrets.token_hex(4)
        self.children: list[subprocess.Popen] = []

    # ---- evidence
    def redact(self, text: str) -> str:
        for value in self.secrets:
            if value:
                text = text.replace(value, "[REDACTED]")
        for pattern in SECRET_PATTERNS:
            text = pattern.sub("[REDACTED]", text)
        return text

    def save(self, name, value):
        payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
        path = self.artifacts / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.redact(payload) + ("\n" if not payload.endswith("\n") else ""))

    def check(self, name, condition, evidence=None):
        row = {"test": self.current_test, "assertion": name, "pass": bool(condition), "evidence": evidence}
        self.assertions.append(row)
        if not condition:
            message = f"{name}: {json.dumps(evidence, ensure_ascii=False, default=str)[:3000]}"
            if getattr(self, "_soft", None) is not None:
                self._soft.append(message)
                return
            raise AssertionError(message)

    @contextlib.contextmanager
    def soft(self):
        """Evaluate every check in the block, then fail with all violated assertions."""
        self._soft = []
        try:
            yield
        finally:
            errors, self._soft = self._soft, None
        if errors:
            raise AssertionError(" || ".join(errors))

    # ---- services
    def docker(self, *args, check=True):
        result = subprocess.run(["docker", *args], capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError("docker " + " ".join(args[:2]) + ": " + result.stderr.strip())
        return result.stdout.strip()

    def start(self):
        password = secrets.token_urlsafe(18)
        access = "i31" + secrets.token_hex(6)
        secret_key = secrets.token_urlsafe(30)
        self.secrets += [password, access, secret_key]
        suffix = secrets.token_hex(3)
        pg, mi = f"issue31-{suffix}-pg", f"issue31-{suffix}-minio"
        self.docker("run", "--rm", "-d", "--name", pg, "-e", "POSTGRES_USER=seal", "-e", "POSTGRES_DB=seal",
                    "-e", f"POSTGRES_PASSWORD={password}", "-p", "127.0.0.1::5432", "--tmpfs", "/var/lib/postgresql/data", PG_IMAGE)
        self.containers.append(pg)
        self.docker("run", "--rm", "-d", "--name", mi, "-e", f"MINIO_ROOT_USER={access}", "-e", f"MINIO_ROOT_PASSWORD={secret_key}",
                    "-p", "127.0.0.1::9000", "--tmpfs", "/data", MINIO_IMAGE, "server", "/data")
        self.containers.append(mi)
        pgport = int(self.docker("port", pg, "5432/tcp").splitlines()[0].rsplit(":", 1)[1])
        s3port = int(self.docker("port", mi, "9000/tcp").splitlines()[0].rsplit(":", 1)[1])
        self.env.update(
            SEAL_DATABASE_URL=f"postgresql://seal:{password}@127.0.0.1:{pgport}/seal",
            SEAL_S3_ENDPOINT=f"http://127.0.0.1:{s3port}",
            SEAL_S3_BUCKET=self.bucket,
            SEAL_S3_ACCESS_KEY=access,
            SEAL_S3_SECRET_KEY=secret_key,
            SEAL_S3_REGION="us-east-1",
        )
        self.s3 = boto3.client("s3", endpoint_url=self.env["SEAL_S3_ENDPOINT"], aws_access_key_id=access,
                               aws_secret_access_key=secret_key, region_name="us-east-1")
        deadline = time.monotonic() + 60
        while True:
            try:
                with self.db() as conn:
                    conn.execute("SELECT 1")
                self.s3.create_bucket(Bucket=self.bucket)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)

    def stop(self):
        for child in self.children:
            with contextlib.suppress(Exception):
                child.terminate()
                child.wait(10)
        for name in self.containers:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    def db(self):
        return psycopg.connect(self.env["SEAL_DATABASE_URL"], row_factory=dict_row, autocommit=True)

    def q(self, sql, params=()):
        with self.db() as conn:
            return conn.execute(sql, params).fetchall()

    def columns(self, table):
        return [r["column_name"] for r in self.q(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='seal' AND table_name=%s ORDER BY ordinal_position", (table,))]

    # ---- S3 / CAS
    def s3_objects(self):
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return keys

    def s3_get(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def cas_key_for(self, digest):
        for key in self.s3_objects():
            if digest in key:
                return key
        return None

    # ---- CLI
    def cli(self, *args, timeout=900, env_extra=None, label=None):
        env = dict(self.env, **(env_extra or {}))
        argv = [sys.executable, "-m", "seal.cli", *map(str, args)]
        started = time.monotonic()
        result = subprocess.run(argv, cwd=ROOT, env=env, text=True, capture_output=True, timeout=timeout)
        row = {"test": self.current_test, "label": label, "argv": ["seal", *map(str, args)], "exit": result.returncode,
               "seconds": round(time.monotonic() - started, 2), "stdout": result.stdout[-20000:], "stderr": result.stderr[-8000:]}
        self.commands.append(row)
        self.save("commands.json", self.commands)
        return result

    def cli_json(self, *args, **kw):
        result = self.cli(*args, **kw)
        return result, parse_json_tail(result.stdout)

    def popen(self, *args, env_extra=None):
        env = dict(self.env, **(env_extra or {}))
        log = open(self.work / f"popen-{len(self.children)}.log", "w")
        child = subprocess.Popen([sys.executable, "-m", "seal.cli", *map(str, args)], cwd=ROOT, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, text=True)
        child.logfile = log.name
        self.children.append(child)
        return child

    # ---- sources
    def source_file(self, source_id, config: dict):
        path = self.work / "sources" / f"{source_id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
        return path


def parse_json_tail(stdout: str):
    """Best effort: parse whole stdout as JSON, else the last JSON object line/block."""
    text = stdout.strip()
    if not text:
        return None
    with contextlib.suppress(Exception):
        return json.loads(text)
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            with contextlib.suppress(Exception):
                return json.loads(line)
    idx = text.rfind("\n{")
    while idx != -1:
        with contextlib.suppress(Exception):
            return json.loads(text[idx + 1:])
        idx = text.rfind("\n{", 0, idx)
    return None


def base_source(source_id, fixture: Fixture, entry_paths, **overrides):
    config = {
        "id": source_id,
        "adapter": "web",
        "entry_urls": [fixture.base + p for p in entry_paths],
        "allowed_hosts": ["127.0.0.1"],
        "allowed_path_prefixes": ["/"],
        "archive_approved": True,
        "scope": "Issue 31 independent acceptance fixture",
        "robots": False,
        "recheck_seconds": 86400,
        "concurrency": 2,
        "budget": {"requests": 200, "seconds": 300, "response_bytes": 20 * 1024 * 1024, "documents": 1000},
        "fetch": {"mode": "http", "max_depth": 3, "page_timeout_ms": 30000, "retries": 0},
        "metadata": {"source_name": f"验收来源 {source_id}", "document_type": "其他", "language": "en"},
        "documents": {"detail_url_patterns": []},
        "parser": {"office_text_policy": "prefer", "quality_accept_routes": ["accept", "accept_ocr", "accept_with_warning"]},
        "ocr": {"engine": "tesseract", "languages": "eng", "dpi": 150},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key] = {**config[key], **value}
        else:
            config[key] = value
    return config


class Api:
    def __init__(self, lab: Lab, callers: dict[str, list[str]]):
        from cryptography.fernet import Fernet

        self.lab = lab
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.keys = {cid: secrets.token_urlsafe(32) for cid in callers}
        lab.secrets += list(self.keys.values())
        config = {
            "public_base_url": self.base,
            "token_key": Fernet.generate_key().decode(),
            "callers": [{"id": cid, "key_sha256": sha256(self.keys[cid].encode()), "sources": srcs} for cid, srcs in callers.items()],
            "request_timeout_seconds": 60,
        }
        lab.secrets.append(config["token_key"])
        self.config_path = lab.work / f"api-{self.port}.local.json"
        self.config_path.write_text(json.dumps(config))
        self.config_path.chmod(0o600)
        self.proc = None

    def start(self):
        self.proc = self.lab.popen("serve", "--config", self.config_path, "--host", "127.0.0.1", "--port", self.port)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with contextlib.suppress(Exception):
                if requests.get(self.base + "/healthz", timeout=2).status_code < 500:
                    return self
            if self.proc.poll() is not None:
                raise RuntimeError("seal serve exited: " + Path(self.proc.logfile).read_text()[-3000:])
            time.sleep(0.5)
        raise RuntimeError("seal serve did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(Exception):
                self.proc.wait(10)

    def get(self, path, caller=None, params=None, raw_auth=None):
        headers = {}
        if caller:
            headers["Authorization"] = "Bearer " + self.keys[caller]
        if raw_auth is not None:
            headers["Authorization"] = raw_auth
        return requests.get(self.base + path, headers=headers, params=params, timeout=60)

    def all_results(self, caller, source_id=None, limit=100):
        items, params, pages = [], {"limit": limit}, 0
        if source_id:
            params["source_id"] = source_id
        while True:
            resp = self.get("/results", caller, params)
            if resp.status_code != 200:
                raise AssertionError(f"/results {resp.status_code} {resp.text[:500]}")
            body = resp.json()
            items += body["items"]
            pages += 1
            if not body.get("next_cursor"):
                return items, pages, body.get("snapshot_id")
            params = {"limit": limit, "cursor": body["next_cursor"]}
            if source_id:
                params["source_id"] = source_id


def run_tests(lab: Lab, tests: list, stop_on=None):
    for func in tests:
        name = func.__name__
        lab.current_test = name
        started = time.monotonic()
        status, detail = "PASS", None
        try:
            func(lab)
        except NotVerified as exc:
            status, detail = "NOT VERIFIED", str(exc)
        except AssertionError as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:  # harness/product crash
            status, detail = "FAIL", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}"
        row = {"test": name, "status": status, "seconds": round(time.monotonic() - started, 1),
               "doc": (func.__doc__ or "").strip().splitlines()[0] if func.__doc__ else "", "detail": lab.redact(detail or "")}
        lab.results.append(row)
        print(f"[{status}] {name} ({row['seconds']}s) {row['detail'][:400]}", flush=True)
        lab.save("results.json", lab.results)
        lab.save("assertions.json", lab.assertions)
