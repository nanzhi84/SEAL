"""Independent Issue #28 E2E support; no production database or bucket is used.

Prerequisites: uv sync --frozen, Docker, postgres:17-alpine and the repository's
seal-minio image, installed Crawl4AI Chromium. Run from repository root with
uv run --no-sync python tests/issue28_acceptance.py.

Failure boundaries (declared before implementation): missing semantic inputs,
runtime noise in fingerprints, changing retries, duplicate delivery/version,
partial commits, current-pointer drift, mutable snapshots, invalid overrides,
mixed concurrent views, cursor loss, unreadable objects, invented legacy facts,
and destructive unique-constraint migration conflict handling.
"""

import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3
import psycopg
import yaml
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]


def source_digest():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src" / "seal").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def reproducibility_files():
    files = [
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        *sorted((ROOT / "tests").glob("issue28*.py")),
    ]
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }


def redact(text):
    text = re.sub(r"(/attachments/)[^\s\"\\<>?]+", r"\1[REDACTED]", text)
    text = re.sub(r"([?&]cursor=)[^\s\"\\<>&]+", r"\1[REDACTED]", text)
    return re.sub(r"gAAAA[A-Za-z0-9_=%-]{50,}", "[REDACTED_CAPABILITY]", text)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pdf_bytes(text):
    """Minimal text-layer PDF; no additional dependency or external fixture."""
    stream = f"BT /F1 14 Tf 40 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    raw, offsets = b"%PDF-1.4\n", [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(raw)
    raw += b"xref\n0 6\n0000000000 65535 f \n"
    raw += b"".join(f"{n:010d} 00000 n \n".encode() for n in offsets[1:])
    return raw + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


class Fixture:
    def __init__(self):
        self.pages = {}
        self.requests = []
        self.block = {}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests.append(self.path)
                if self.path in fixture.block:
                    fixture.block[self.path].wait(30)
                response = fixture.pages.get(self.path, (404, "text/plain", b"missing"))
                status, media, body = response[:3]
                self.send_response(status)
                self.send_header("Content-Type", media)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (response[3] if len(response) > 3 else {}).items():
                    self.send_header(key, value)
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def html(self, path, text, links=(), status=200, script=""):
        body = (
            f"<!doctype html><html><head><title>Issue28 fixture</title></head><body><main>"
            f"<h1>{text}</h1><p>{text} public acceptance content with enough meaningful words "
            "for stable parser behavior and actual downstream content checks.</p>"
        )
        body += "".join(f'<a href="{x}">{x}</a>' for x in links)
        self.pages[path] = (
            status,
            "text/html; charset=utf-8",
            (body + script + "</main></body></html>").encode(),
        )


class Runtime:
    def __init__(self, name="issue28"):
        self.name = (
            name
            + "-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-"
            + secrets.token_hex(3)
        )
        self.work = ROOT / "work" / self.name
        self.artifacts = ROOT / "artifacts" / self.name
        self.work.mkdir(parents=True)
        self.artifacts.mkdir(parents=True)
        self.env = dict(os.environ)
        for key in tuple(self.env):
            if key.startswith("SEAL_") or key.startswith("AWS_"):
                del self.env[key]
        self.env.update(
            SEAL_ALLOW_LOOPBACK="1",
            SEAL_WORK_DIR=str(self.work / "runtime"),
            PYTHONPATH=str(ROOT / "src"),
        )
        self.containers, self.children, self.checks, self.commands = [], [], [], []
        self.fixture = Fixture()
        self.s3 = None
        self.source_digest = source_digest()
        self.reproduction = reproducibility_files()
        self.secret_values = []

    def protect(self, *values):
        self.secret_values.extend(values)
        path = self.work / ".secrets.local.json"
        path.write_text(json.dumps(self.secret_values))
        path.chmod(0o600)

    def save(self, name, value):
        payload = redact(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        for value in self.secret_values:
            payload = payload.replace(value, "[REDACTED_SECRET]")
        (self.artifacts / name).write_text(payload + "\n")

    def check(self, name, condition, evidence=None):
        row = {"assertion": name, "pass": bool(condition), "evidence": evidence}
        self.checks.append(row)
        self.save("assertions.json", self.checks)
        if not condition:
            raise AssertionError(name + ": " + str(evidence))

    def docker(self, *args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("Docker operation failed: " + result.stderr.strip())
        return result.stdout.strip()

    def start(self):
        password, access, secret = (
            secrets.token_urlsafe(24),
            "issue28" + secrets.token_hex(5),
            secrets.token_urlsafe(30),
        )
        self.protect(password, access, secret)
        image = "seal-minio:RELEASE.2025-10-15T17-29-55Z"
        found = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
        if found.returncode:
            build_env = dict(
                self.env,
                MINIO_ROOT_USER=access,
                MINIO_ROOT_PASSWORD=secret,
                SEAL_POSTGRES_PASSWORD=password,
                SEAL_S3_ACCESS_KEY=access,
                SEAL_S3_SECRET_KEY=secret,
            )
            subprocess.run(
                ["docker", "compose", "build", "minio"],
                cwd=ROOT,
                env=build_env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
        pg = self.name + "-pg"
        self.containers.append(pg)
        self.docker(
            "run",
            "--rm",
            "-d",
            "--name",
            pg,
            "-e",
            "POSTGRES_USER=seal",
            "-e",
            "POSTGRES_DB=seal",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-p",
            "127.0.0.1::5432",
            "--tmpfs",
            "/var/lib/postgresql/data",
            "postgres:17-alpine",
        )
        mi = self.name + "-minio"
        self.containers.append(mi)
        self.docker(
            "run",
            "--rm",
            "-d",
            "--name",
            mi,
            "-e",
            f"MINIO_ROOT_USER={access}",
            "-e",
            f"MINIO_ROOT_PASSWORD={secret}",
            "-p",
            "127.0.0.1::9000",
            "--tmpfs",
            "/data",
            "seal-minio:RELEASE.2025-10-15T17-29-55Z",
            "server",
            "/data",
        )
        pgport = int(self.docker("port", pg, "5432/tcp").rsplit(":", 1)[1])
        s3port = int(self.docker("port", mi, "9000/tcp").rsplit(":", 1)[1])
        self.env.update(
            SEAL_DATABASE_URL=f"postgresql://seal:{password}@127.0.0.1:{pgport}/seal",
            SEAL_S3_ENDPOINT=f"http://127.0.0.1:{s3port}",
            SEAL_S3_BUCKET="issue28",
            SEAL_S3_ACCESS_KEY=access,
            SEAL_S3_SECRET_KEY=secret,
            SEAL_S3_REGION="us-east-1",
        )
        self.s3 = boto3.client(
            "s3",
            endpoint_url=self.env["SEAL_S3_ENDPOINT"],
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name="us-east-1",
        )
        deadline = time.monotonic() + 40
        while True:
            try:
                with self.db() as conn:
                    conn.execute("SELECT 1")
                self.s3.create_bucket(Bucket="issue28")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.25)
        self.save(
            "environment.json",
            {
                "postgres": "postgres:17-alpine",
                "minio": "seal-minio:RELEASE.2025-10-15T17-29-55Z",
                "isolation": "ephemeral containers with tmpfs; dedicated bucket; loopback controlled sources",
                "python": sys.version,
                "git_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "source_sha256": self.source_digest,
                "reproducibility_sha256": self.reproduction,
                "reproduce": "uv sync --frozen && uv run --no-sync python tests/issue28_acceptance.py",
                "source": self.fixture.base,
                "work": str(self.work),
                "artifacts": str(self.artifacts),
            },
        )

    def db(self, database=None):
        dsn = self.env["SEAL_DATABASE_URL"]
        if database:
            dsn = dsn.rsplit("/", 1)[0] + "/" + database
        return psycopg.connect(dsn, row_factory=dict_row)

    def query(self, sql, params=()):
        with self.db() as conn:
            return conn.execute(sql, params).fetchall()

    def cli(self, *args, env=None, success=True, timeout=120):
        chosen = env or self.env
        result = subprocess.run(
            [sys.executable, "-m", "seal", *map(str, args)],
            cwd=ROOT,
            env=chosen,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        row = {
            "argv": ["python", "-m", "seal", *map(str, args)],
            "exit": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        self.commands.append(row)
        self.save("commands.json", self.commands)
        if success and result.returncode != 0:
            raise AssertionError("CLI failed: " + json.dumps(row))
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = {"raw": result.stdout}
        return result.returncode, data

    def config(self, source_id, paths, **changes):
        config = {
            "id": source_id,
            "entry_urls": [self.fixture.base + x for x in paths],
            "allowed_hosts": ["127.0.0.1"],
            "allowed_path_prefixes": ["/"],
            "archive_approved": True,
            "scope": "Isolated Issue28 acceptance fixtures",
            "robots": False,
            "recheck_seconds": 1,
            "concurrency": 1,
            "budget": {"requests": 60, "seconds": 60, "response_bytes": 2000000},
            "crawl4ai": {
                "mode": "http",
                "max_depth": 1,
                "page_timeout_ms": 10000,
                "word_count_threshold": 1,
            },
        }
        config.update(changes)
        path = self.work / (source_id + ".yaml")
        path.write_text(yaml.safe_dump(config))
        return path

    def collect(self, config, **kwargs):
        return self.cli("collect", config, "--no-progress", **kwargs)

    def versions(self, source_id):
        return self.query(
            "SELECT v.*,r.url FROM seal.version v JOIN seal.resource r ON r.id=v.resource_id "
            "JOIN seal.frontier f ON f.id=r.frontier_id WHERE f.source_id=%s ORDER BY v.cursor",
            (source_id,),
        )

    def observations(self, source_id):
        return self.query(
            "SELECT o.* FROM seal.observation o JOIN seal.resource r ON r.id=o.resource_id "
            "JOIN seal.frontier f ON f.id=r.frontier_id WHERE f.source_id=%s ORDER BY o.observed_at,o.id",
            (source_id,),
        )

    def state(self):
        return {
            table: self.query(f"SELECT * FROM seal.{table} ORDER BY 1")
            for table in (
                "source",
                "resource",
                "observation",
                "version",
                "snapshot",
                "snapshot_source",
                "snapshot_item",
                "publication",
            )
        }

    def archive(self, version):
        raw = self.s3.get_object(Bucket="issue28", Key=version["result_ref"])["Body"].read()
        self.check(
            "result object CAS digest", hashlib.sha256(raw).hexdigest() == version["result_ref"]
        )
        return json.loads(raw)

    def spawn(self, *args, env=None):
        log = (self.work / f"process-{len(self.children)}.log").open("w")
        proc = subprocess.Popen(
            [sys.executable, "-m", "seal", *map(str, args)],
            cwd=ROOT,
            env=env or self.env,
            stdout=log,
            stderr=log,
        )
        self.children.append(proc)
        return proc

    def close(self):
        for event in self.fixture.block.values():
            event.set()
        for proc in self.children:
            if proc.poll() is None:
                proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(10)
                if proc.poll() is None:
                    proc.kill()
        self.fixture.server.shutdown()
        for name in reversed(self.containers):
            with contextlib.suppress(Exception):
                self.docker("rm", "-f", name)
        self.save("assertions.json", self.checks)

    def __enter__(self):
        try:
            self.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, typ, error, _):
        stable = self.source_digest == source_digest()
        tests_stable = self.reproduction == reproducibility_files()
        if typ is not None:
            with contextlib.suppress(Exception):
                self.save("failed-state.json", self.state())
        self.save(
            "result.json",
            {
                "pass": typ is None and stable and tests_stable,
                "error": str(error) if error else None,
                "source_stable": stable,
                "tests_and_dependencies_stable": tests_stable,
                "source_sha256": source_digest(),
                "assertions": len(self.checks),
                "passed": sum(x["pass"] for x in self.checks),
            },
        )
        self.close()
        print(self.artifacts)
        if not (stable and tests_stable) and typ is None:
            raise AssertionError("source or acceptance dependencies changed; rerun on frozen files")
