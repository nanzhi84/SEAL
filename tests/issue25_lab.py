"""Real CLI, isolated PostgreSQL/S3, and independently collected evidence."""

import hashlib
import importlib.metadata
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3
import psycopg
import yaml
from cryptography.fernet import Fernet
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]


def digest(body):
    return hashlib.sha256(body).hexdigest()


def implementation_digest():
    result = hashlib.sha256()
    paths = sorted((ROOT / "src" / "seal").rglob("*.py")) + [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for path in paths:
        result.update(str(path.relative_to(ROOT)).encode())
        result.update(path.read_bytes())
    return result.hexdigest()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def local_settings():
    env = os.environ.copy()
    path = ROOT / ".env.local"
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            parts = shlex.split(value)
            env.setdefault(key.strip(), parts[0] if parts else "")
    return env


class Lab:
    def __init__(self, name):
        self.name = name
        self.work = ROOT / "work" / name
        self.out = ROOT / "artifacts" / name
        self.work.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=True)
        self.env = local_settings()
        tessdata = ROOT / "work" / "ocr-tessdata"
        if tessdata.is_dir():
            self.env.setdefault("TESSDATA_PREFIX", str(tessdata))
        self.environment = {
            "python": sys.version.split()[0],
            "implementation_sha256": implementation_digest(),
            "dependencies": {name: importlib.metadata.version(name) for name in
                             ["crawl4ai", "pypdf", "pypdfium2", "psycopg", "boto3"]},
            "models": [],
        }
        if executable := shutil.which("tesseract"):
            result = subprocess.run([executable, "--version"], capture_output=True, text=True)
            self.environment["ocr_engine"] = result.stdout.splitlines()[0] if result.stdout else "unavailable"
        for language in ["eng", "chi_sim", "vie"]:
            path = Path(self.env.get("TESSDATA_PREFIX", "")) / (language + ".traineddata")
            if path.is_file():
                model = path.read_bytes()
                self.environment["models"].append({"language": language, "bytes": len(model), "sha256": digest(model)})
        unique = secrets.token_hex(5)
        self.database = "seal_issue25_" + unique
        self.bucket = "seal-issue25-" + unique
        base_dsn = self.env["SEAL_DATABASE_URL"]
        with psycopg.connect(base_dsn, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(self.database)))
        options = conninfo_to_dict(base_dsn)
        options["dbname"] = self.database
        self.env["SEAL_DATABASE_URL"] = make_conninfo(**options)
        self.s3 = boto3.client(
            "s3", endpoint_url=self.env["SEAL_S3_ENDPOINT"],
            aws_access_key_id=self.env.get("MINIO_ROOT_USER", self.env["SEAL_S3_ACCESS_KEY"]),
            aws_secret_access_key=self.env.get("MINIO_ROOT_PASSWORD", self.env["SEAL_S3_SECRET_KEY"]),
            region_name=self.env.get("SEAL_S3_REGION", "us-east-1"),
        )
        self.s3.create_bucket(Bucket=self.bucket)
        self.env.update({
            "SEAL_S3_BUCKET": self.bucket,
            "SEAL_S3_ACCESS_KEY": self.env.get("MINIO_ROOT_USER", self.env["SEAL_S3_ACCESS_KEY"]),
            "SEAL_S3_SECRET_KEY": self.env.get("MINIO_ROOT_PASSWORD", self.env["SEAL_S3_SECRET_KEY"]),
            "SEAL_ALLOW_LOOPBACK": "1", "SEAL_WORK_DIR": str(self.work / "runtime"),
        })
        self.checks = []
        self.commands = []
        self.processes = []
        self.source_ids = []

    def check(self, name, passed, evidence=None):
        self.checks.append({"name": name, "status": "PASS" if passed else "FAIL", "evidence": evidence})
        self.save()

    def save(self):
        report = {
            "contract": "https://github.com/nanzhi84/SEAL/issues/25",
            "run": self.name, "database": self.database, "bucket": self.bucket,
            "environment": self.environment,
            "command": "uv run --no-sync python tests/" + Path(sys.argv[0]).name + " --name " + self.name,
            "prerequisites": "macOS fonts; project Python 3.12 dependencies and configured actual OCR engine; .env.local PostgreSQL/S3 admin credentials; existing local services. Fresh isolated database and bucket created each run.",
            "checks": self.checks, "commands": self.commands,
            "unverified": ["production/public-source deployment", "parent SIGKILL or machine power loss instantaneous cleanup"],
        }
        (self.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    def cli(self, *args, timeout=240, env=None):
        started = time.monotonic()
        proc = subprocess.run([sys.executable, "-m", "seal", *map(str, args)], cwd=ROOT,
                              env=env or self.env, capture_output=True, timeout=timeout)
        receipt = None
        for line in proc.stdout.decode(errors="replace").splitlines()[::-1]:
            try:
                receipt = json.loads(line)
                break
            except ValueError:
                pass
        self.commands.append({"args": list(map(str, args)), "exit": proc.returncode,
                              "seconds": round(time.monotonic() - started, 3), "receipt": receipt,
                              "stderr_bytes": len(proc.stderr), "stdout_bytes": len(proc.stdout)})
        self.save()
        return proc, receipt

    def connect(self):
        return psycopg.connect(self.env["SEAL_DATABASE_URL"], row_factory=dict_row)

    def object(self, key):
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        try:
            body = response["Body"].read()
        finally:
            response["Body"].close()
        if digest(body) != key:
            raise AssertionError("S3 content-address mismatch")
        return body

    def source(self, source_id, urls, *, seconds=180, timeout_ms=30000, extra=None):
        from urllib.parse import urlsplit
        value = {
            "id": source_id, "entry_urls": urls,
            "allowed_hosts": sorted({urlsplit(url).hostname for url in urls}),
            "allowed_path_prefixes": ["/"], "archive_approved": True,
            "scope": "isolated Issue 25 synthetic non-sensitive fixture", "robots": False,
            "concurrency": 8, "delay": 0.0,
            "budget": {"requests": 100, "seconds": seconds, "response_bytes": 10485760},
            "crawl4ai": {"mode": "http", "max_depth": 0, "page_timeout_ms": timeout_ms,
                         "word_count_threshold": 0},
            "ocr": {"languages": "eng+chi_sim+vie", "timeout_seconds": 180,
                    "max_pages": 200, "dpi": 144, "max_page_pixels": 20000000,
                    "max_result_bytes": 16777216},
        }
        if extra:
            value.update(extra)
        path = self.work / (source_id + ".yaml")
        path.write_text(yaml.safe_dump(value, allow_unicode=True))
        (self.out / (source_id + ".yaml")).write_bytes(path.read_bytes())
        self.source_ids.append(source_id)
        return path

    def start_api(self):
        port = free_port()
        self.base = "http://127.0.0.1:" + str(port)
        self.bearer = secrets.token_urlsafe(32)
        config = {"public_base_url": self.base, "token_key": Fernet.generate_key().decode(),
                  "callers": [{"id": "issue25", "key_sha256": digest(self.bearer.encode()),
                               "sources": self.source_ids}]}
        path = self.work / "api.local.json"
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        log = open(self.work / "api.log", "wb")
        proc = subprocess.Popen([sys.executable, "-m", "seal", "serve", "--config", str(path),
                                 "--port", str(port)], cwd=ROOT, env=self.env, stdout=log, stderr=log)
        self.processes.append((proc, log))
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError("API startup failed")
            try:
                self.get("/sources")
                return
            except (OSError, urllib.error.HTTPError):
                time.sleep(0.1)
        raise RuntimeError("API startup timeout")

    def get(self, path, *, binary=False):
        url = path if path.startswith("http") else self.base + path
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.bearer})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
        return body if binary else json.loads(body)

    def close(self):
        for proc, log in self.processes:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            log.close()
        self.s3.close()
        self.save()
