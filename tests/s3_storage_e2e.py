"""独立对象存储业务验收；uv run --no-sync python tests/s3_storage_e2e.py。

失败方式：错误/遗漏 S3 配置、鉴权拒绝、服务不可达、对象缺失/损坏、
相同内容重复上传和竞争、结果/附件不可跨进程重建、失败仍写入成功版本、
init 先写 DB 后发现对象存储不可用、临时文件落入稳定存储。
仅使用隔离 Docker DB/MinIO 和授权 loopback 合成来源；不触碰稳定存储。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import boto3
import psycopg
import yaml
from botocore.config import Config
from psycopg.rows import dict_row

from issue17_fixture import Fixture, html_bytes

ROOT = Path(__file__).resolve().parents[1]
MINIO_IMAGE = "seal-minio:RELEASE.2025-10-15T17-29-55Z"


def digest(body):
    return hashlib.sha256(body).hexdigest()


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")


class Lab:
    def __init__(self):
        self.name = "s3-storage-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:5]
        self.work = ROOT / "work" / self.name
        self.artifacts = ROOT / "artifacts" / self.name
        self.work.mkdir(parents=True)
        self.artifacts.mkdir(parents=True)
        self.containers = []
        self.checks = []
        self.commands = []
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SEAL_")}
        # Random ephemeral keys are kept only in memory and Docker's ephemeral metadata.
        self.access = "e2e" + uuid.uuid4().hex[:16]
        self.secret = uuid.uuid4().hex
        self.app_access = "app" + uuid.uuid4().hex[:16]
        self.app_secret = uuid.uuid4().hex
        self.bucket = "seal-e2e"
        self.fixture = Fixture(self.work / "fixture")
        self.fixture.root_links = {"/a/": ("/article?edition=1", "/file.pdf")}

    def check(self, name, value, **evidence):
        self.checks.append({"assertion": name, "status": "PASS" if value else "FAIL", **evidence})
        print(self.checks[-1]["status"], name, flush=True)
        write(self.artifacts / "assertions.json", self.checks)

    def sql(self, query, params=()):
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as c:
            result = c.execute(query, params)
            return result.fetchall() if result.description else []

    def docker(self, *args):
        return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.PIPE).strip()

    def start(self):
        dbname = self.name + "-pg"
        self.containers.append(dbname)
        self.docker("run", "-d", "--name", dbname, "-p", "127.0.0.1::5432", "-e",
                    "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=seal_e2e",
                    "-v", f"{self.work / 'postgres'}:/var/lib/postgresql/data", "postgres:17-alpine")
        port = self.docker("port", dbname, "5432").split(":")[-1]
        self.dsn = f"postgresql://postgres@127.0.0.1:{port}/seal_e2e"
        minio = self.name + "-minio"
        self.minio = minio
        self.containers.append(minio)
        self.docker("run", "-d", "--name", minio, "-p", "127.0.0.1::9000",
                    "-e", "MINIO_ROOT_USER=" + self.access, "-e", "MINIO_ROOT_PASSWORD=" + self.secret,
                    "-v", f"{self.work / 'minio'}:/data", MINIO_IMAGE, "server", "/data")
        port = self.docker("port", minio, "9000").split(":")[-1]
        self.endpoint = f"http://127.0.0.1:{port}"
        self.s3 = boto3.client("s3", endpoint_url=self.endpoint,
                               aws_access_key_id=self.access, aws_secret_access_key=self.secret,
                               region_name="us-east-1", config=Config(retries={"max_attempts": 0},
                               connect_timeout=2, read_timeout=3, s3={"addressing_style": "path"}))
        deadline = time.monotonic() + 50
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
        self.env.update(SEAL_DATABASE_URL=self.dsn, SEAL_S3_ENDPOINT=self.endpoint,
                        SEAL_S3_BUCKET=self.bucket, SEAL_S3_ACCESS_KEY=self.access,
                        SEAL_S3_SECRET_KEY=self.secret, SEAL_WORK_DIR=str(self.work / "seal"),
                        SEAL_ALLOW_LOOPBACK="1", NO_COLOR="1")
        self.application_user()
        self.fixture.start()
        write(self.artifacts / "environment.json", {
            "command": "uv run --no-sync python tests/s3_storage_e2e.py",
            "postgres_image": "postgres:17-alpine", "minio_image": MINIO_IMAGE,
            "python": sys.version, "source": "dd-017 synthetic isolated loopback replacement",
            "scope": "CLI→SQL→real S3 API; synthetic HTML/PDF; no external site coverage",
            "storage": "work/<run>/postgres and work/<run>/minio",
            "credentials": "ephemeral randomized keys omitted", "region": "default us-east-1",
            "git_head": self.docker_git(),
            "test_sha256": digest(Path(__file__).read_bytes()),
            "product_source_sha256": {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sorted((ROOT / "src/seal").rglob("*.py"))},
            "minio_image_id": self.docker("image", "inspect", MINIO_IMAGE, "--format", "{{.Id}}"),
            "minio_build_precondition": "Image built locally from official minio RELEASE.2025-10-15T17-29-55Z source and fixes; Docker and fixed mc image available",
            "network": "HTTP fixture and service ports bind 127.0.0.1; mc uses isolated MinIO container network",
        })

    def application_user(self):
        policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::" + self.bucket]},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": ["arn:aws:s3:::" + self.bucket + "/*"]}]}
        write(self.work / "app-policy.json", policy)
        script = 'mc alias set isolated http://127.0.0.1:9000 "$ROOT_ACCESS" "$ROOT_SECRET" >/dev/null && mc admin user add isolated "$APP_ACCESS" "$APP_SECRET" >/dev/null && mc admin policy create isolated limited /input/app-policy.json >/dev/null && mc admin policy attach isolated limited --user "$APP_ACCESS" >/dev/null'
        self.docker("run", "--rm", "--network", "container:" + self.minio,
                    "-v", f"{self.work}:/input:ro", "-e", "ROOT_ACCESS=" + self.access,
                    "-e", "ROOT_SECRET=" + self.secret, "-e", "APP_ACCESS=" + self.app_access,
                    "-e", "APP_SECRET=" + self.app_secret, "--entrypoint", "/bin/sh",
                    "minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727", "-c", script)
        self.env.update(SEAL_S3_ACCESS_KEY=self.app_access, SEAL_S3_SECRET_KEY=self.app_secret)
        app = boto3.client("s3", endpoint_url=self.endpoint, aws_access_key_id=self.app_access,
                           aws_secret_access_key=self.app_secret, region_name="us-east-1",
                           config=Config(retries={"max_attempts": 0}, s3={"addressing_style": "path"}))
        self.s3.create_bucket(Bucket="other-synthetic-bucket")
        self.s3.put_object(Bucket=self.bucket, Key="policy-fixture", Body=b"policy")
        for label, action in (("DeleteObject", lambda: app.delete_object(Bucket=self.bucket, Key="policy-fixture")),
                              ("other bucket", lambda: app.list_objects_v2(Bucket="other-synthetic-bucket")),
                              ("other bucket PutObject", lambda: app.put_object(Bucket="other-synthetic-bucket", Key="forbidden", Body=b"synthetic"))):
            try:
                action()
                denied = False
            except Exception as exc:
                denied = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode") == 403
            self.check("application policy denies " + label, denied)
        self.s3.delete_object(Bucket=self.bucket, Key="policy-fixture")
        admin = 'mc alias set app http://127.0.0.1:9000 "$APP_ACCESS" "$APP_SECRET" >/dev/null && mc admin user list app --json'
        cmd = ["docker", "run", "--rm", "--network", "container:" + self.minio,
               "-e", "APP_ACCESS=" + self.app_access, "-e", "APP_SECRET=" + self.app_secret,
               "--entrypoint", "/bin/sh", "minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727", "-c", admin]
        result = subprocess.run(cmd, text=True, capture_output=True)
        text = self.clean(result.stdout + result.stderr)
        write(self.artifacts / "application-admin-denied.json", {"exit": result.returncode, "output": text})
        self.check("application user administration denied", result.returncode != 0 or 'Access Denied' in text or 'AccessDenied' in text)
        visible = app.list_buckets().get("Buckets", [])
        self.check("application bucket listing cannot expose other bucket", all(b["Name"] == self.bucket for b in visible), buckets=[b["Name"] for b in visible])
        app.close()

    def docker_git(self):
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()

    def clean(self, text):
        return text.replace(self.access, "REDACTED").replace(self.secret, "REDACTED").replace(self.app_access, "REDACTED").replace(self.app_secret, "REDACTED")

    def cli(self, *args, env=None, expected=0):
        return self.process(["-m", "seal.cli", *map(str, args)], env=env, expected=expected)

    def process(self, args, env=None, expected=0):
        cmd = [sys.executable, *args]
        p = subprocess.run(cmd, cwd=ROOT, env=env or self.env, capture_output=True, text=True, timeout=180)
        stdout, stderr = self.clean(p.stdout), self.clean(p.stderr)
        number = len(self.commands) + 1
        write(self.artifacts / f"{number:03d}-receipt.json", {"stdout": stdout, "stderr": stderr})
        self.commands.append({"argv": cmd, "exit": p.returncode})
        self.check(f"command {number} exit", p.returncode == expected if expected == 0 else p.returncode != 0,
                   actual=p.returncode, expected="success" if expected == 0 else "failure")
        try:
            receipt = json.loads(stdout)
        except ValueError:
            receipt = {}
        if expected:
            self.check(f"command {number} explicit failure", bool(receipt.get("error") or
                       receipt.get("errors") or receipt.get("status") in ("failed", "retryable")), receipt=receipt)
        return p.returncode, receipt

    def source(self, paths=("/a/",)):
        path = self.work / "dd-017.yaml"
        config = dict(id="dd-017", entry_urls=[self.fixture.base + p for p in paths],
                      allowed_hosts=["127.0.0.1"], archive_approved=True,
                      scope="Authorized synthetic loopback acceptance only", robots=False,
                      recheck_seconds=0, delay=0.0, concurrency=2,
                      budget=dict(requests=20, seconds=60, response_bytes=1048576),
                      crawl4ai=dict(mode="http", max_depth=2, word_count_threshold=1))
        path.write_text(yaml.safe_dump(config))
        (self.artifacts / "dd-017.yaml").write_text(path.read_text())
        return path

    def versions(self):
        return self.sql("SELECT * FROM seal.version_delta ORDER BY cursor")

    def count(self):
        return self.sql("SELECT count(*) AS n FROM seal.version")[0]["n"]

    def body(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def objects(self):
        return [o["Key"] for o in self.s3.list_objects_v2(Bucket=self.bucket).get("Contents", [])]

    def reconstruct(self, expected=0):
        script = '''import json,hashlib,os
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from seal.archive import deserialize_result
with psycopg.connect(os.environ["SEAL_DATABASE_URL"],row_factory=dict_row) as c:
 rows=c.execute("SELECT * FROM seal.version_delta ORDER BY cursor").fetchall()
results=[]
for row in rows:
 r=deserialize_result(row["crawl_result"])
 files=[{"hash":hashlib.sha256(Path(p).read_bytes()).hexdigest(),"path":p} for p in r.downloaded_files or []]
 results.append({"url":r.url,"success":r.success,"html":r.html,"markdown":str(r.markdown),"files":files,"pdf":bool(r.pdf)})
print(json.dumps({"results":results}))'''
        return self.process(["-c", script], expected=expected)

    def execute(self):
        # init must inspect S3 before creating any schema in the empty database.
        bad = {**self.env, "SEAL_S3_ENDPOINT": "http://127.0.0.1:1"}
        before = self.sql("SELECT table_schema,table_name FROM information_schema.tables ORDER BY 1,2")
        self.cli("init", env=bad, expected=1)
        after = self.sql("SELECT table_schema,table_name FROM information_schema.tables ORDER BY 1,2")
        self.check("init unavailable leaves database unchanged", before == after)
        for label, overrides in (("auth", {"SEAL_S3_SECRET_KEY": "wrong"}),
                                 ("missing-config", {"SEAL_S3_BUCKET": ""})):
            self.cli("init", env={**self.env, **overrides}, expected=1)
            current = self.sql("SELECT table_schema,table_name FROM information_schema.tables ORDER BY 1,2")
            self.check("init " + label + " leaves database unchanged", before == current)
        self.cli("init")
        self.legacy_import()
        path = self.source()
        self.cli("collect", path, "--no-progress")
        rows = self.versions()
        self.check("CLI produced HTML and PDF versions", len(rows) >= 3, versions=len(rows))
        self.check("Postgres source/frontier/run/resource relationships", bool(self.sql(
            "SELECT s.id AS source_id,f.id FROM seal.source s JOIN seal.frontier f ON f.source_id=s.id"
        )))
        keys = self.objects()
        self.check("all S3 object keys flat SHA256", bool(keys) and all(re.fullmatch(r"[a-f0-9]{64}", k) for k in keys), keys=keys)
        for key in keys:
            self.check("S3 bytes match content key " + key[:12], digest(self.body(key)) == key)
        for row in rows:
            original = self.body(row["body_hash"])
            request = next((r for r in self.fixture.requests if self.fixture.base + r["path"] == row["url"]), {})
            self.check("original hash equals authorized HTTP bytes " + row["url"], digest(original) == request.get("sha256"))
        write(self.artifacts / "versions.json", rows)
        _, result = self.reconstruct()
        reconstructed = result.get("results", [])
        self.check("new process reconstructs complete native results", len(reconstructed) == len(rows) and
                   all(r["success"] and r["markdown"] for r in reconstructed))
        self.check("PDF downstream result retains native evidence", any("file.pdf" in r["url"] and
                   "SEAL PDF original sample" in r["markdown"] for r in reconstructed))
        n = self.count()
        self.cli("recheck", path, "--no-progress")
        self.check("unchanged recheck creates no version", self.count() == n)
        self.check("unchanged observations persisted", bool(self.sql("SELECT id FROM seal.observation WHERE outcome='unchanged'")))
        self.fixture.set("/article?edition=1", body=html_bytes("Changed", "New synthetic content."))
        self.cli("recheck", path, "--enqueue")
        self.cli("worker", "--once")
        self.check("queued worker stores changed version", self.count() == n + 1)
        self.reconstruct()
        self.cas_and_failures(path)

    def cas_and_failures(self, path):
        # Exercise public CAS API in separate concurrent processes, observe through S3.
        payload = "parallel synthetic durable CAS bytes"
        key = digest(payload.encode())
        script = "from seal.objects import Objects; import json; print(json.dumps({'key':Objects().put(" + repr(payload.encode()) + ")}))"
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            outputs = list(pool.map(lambda _: subprocess.run([sys.executable, "-c", script],
                        cwd=ROOT, env=self.env, text=True, capture_output=True, timeout=30), range(12)))
        self.check("duplicate concurrent CAS one durable content key", all(p.returncode == 0 and
                   json.loads(p.stdout)["key"] == key for p in outputs) and self.objects().count(key) == 1
                   and self.body(key) == payload.encode())
        baseline = self.count()
        for label, overrides in (("authentication", {"SEAL_S3_SECRET_KEY": "wrong-ephemeral-secret"}),
                                 ("unavailable", {"SEAL_S3_ENDPOINT": "http://127.0.0.1:1"})):
            self.fixture.set("/article?edition=1", body=html_bytes(label, "Failure must not create successful version."))
            self.cli("collect", path, "--no-progress", env={**self.env, **overrides}, expected=1)
            self.check(label + " cannot create success version", self.count() == baseline)
        # Missing/corrupt historical evidence must make a new process fail explicitly.
        key = self.versions()[0]["body_hash"]
        saved = self.body(key)
        read = "from seal.objects import Objects\nfrom seal.core import SealError;import json\ntry: Objects().get(" + repr(key) + ");print(json.dumps({'success':True}))\nexcept SealError as e: print(json.dumps({'error':e.code}));raise SystemExit(2)"
        for label, mutation in (("missing", lambda: self.s3.delete_object(Bucket=self.bucket, Key=key)),
                                ("corrupt", lambda: self.s3.put_object(Bucket=self.bucket, Key=key, Body=b"corrupt"))):
            mutation()
            self.process(["-c", read], expected=1)
            self.check(label + " cannot mutate successful versions", self.count() == baseline)
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=saved)
        self.attachment_roundtrip()

    def attachment_roundtrip(self):
        # Public archive API: native CrawlResult binary fields/download persist via S3.
        script = '''import json,base64,os
from pathlib import Path
from crawl4ai import CrawlResult
from seal.archive import serialize_result
from seal.objects import Objects
p=Path(os.environ["SEAL_WORK_DIR"])/"synthetic-download.bin"
p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b"attachment original bytes")
r=CrawlResult(url="https://example.org/synthetic",html="<p>authorized</p>",success=True,downloaded_files=[str(p)],pdf=b"synthetic PDF bytes",screenshot=base64.b64encode(b"synthetic screenshot bytes").decode(),mhtml="synthetic mhtml")
e=serialize_result(r);key=Objects().put_json(e)
p.unlink();print(json.dumps({"key":key,"envelope":e}))'''
        _, result = self.process(["-c", script])
        write(self.artifacts / "attachment-envelope.json", result)
        key = result.get("key")
        script = '''import json,hashlib,os
from pathlib import Path
from seal.archive import deserialize_result
from seal.objects import Objects
r=deserialize_result(Objects().json(KEY))
p=Path(r.downloaded_files[0]);print(json.dumps({"attachment":p.read_bytes().decode(),"path":str(p),"pdf":r.pdf.decode(),"mhtml":r.mhtml,"screenshot":r.screenshot}))'''.replace("KEY", repr(key))
        _, got = self.process(["-c", script])
        self.check("attachment restored from S3 in new process", got.get("attachment") == "attachment original bytes"
                   and got.get("pdf") == "synthetic PDF bytes" and got.get("mhtml") == "synthetic mhtml")
        restored = Path(got.get("path", "/missing")).resolve()
        self.check("restored attachment confined to work directory", restored.is_relative_to(self.work))
        self.check("attachment S3 original bytes", digest(b"attachment original bytes") in self.objects()
                   and self.body(digest(b"attachment original bytes")) == b"attachment original bytes")
        shutil.rmtree(self.work / "seal" / "downloads", ignore_errors=True)
        _, fresh = self.process(["-c", script])
        self.check("work download cache deletion survives new-process reconstruction",
                   fresh.get("attachment") == "attachment original bytes")
        self.s3.delete_object(Bucket=self.bucket, Key=digest(b"attachment original bytes"))
        script = "from seal.core import SealError\ntry:\n" + "\n".join(" " + line for line in script.splitlines()) + "\nexcept SealError as e: print(json.dumps({'error':e.code}));raise SystemExit(2)"
        self.process(["-c", script], expected=1)

    def legacy_import(self):
        legacy = self.work / "synthetic-legacy"
        originals = {}
        for body in (b"legacy synthetic one", b"legacy synthetic two"):
            key = digest(body)
            path = legacy / "objects" / key[:2] / key[2:]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            originals[key] = body
        for attempt in range(2):
            self.process(["-m", "seal.archive_import", str(legacy)])
            self.check("legacy import key preservation run " + str(attempt + 1),
                       all(self.body(k) == b for k, b in originals.items()))
            self.check("legacy original files retained run " + str(attempt + 1),
                       all((legacy / "objects" / k[:2] / k[2:]).read_bytes() == b for k, b in originals.items()))
        self.cli("init", env={**self.env, "SEAL_ARCHIVE": str(legacy)}, expected=1)

    def close(self):
        sdk = self.work / "seal" / "sdk"
        if sdk.exists():
            files = [{"path": str(p.relative_to(self.work)), "bytes": p.stat().st_size,
                      "sha256": digest(p.read_bytes())} for p in sorted(sdk.rglob("*")) if p.is_file()]
            write(self.artifacts / "sdk-work-files.json", files)
            self.check("SDK cache and diagnostic files observed inside isolated work",
                       bool(files) and all(row["path"].startswith("seal/sdk/") for row in files), files=files)
        if self.fixture.thread.is_alive():
            self.fixture.close()
        else:
            self.fixture.server.server_close()
        for name in self.containers:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        write(self.artifacts / "commands.json", self.commands)
        write(self.artifacts / "summary.json", {"run": self.name, "assertions": self.checks,
              "pass": sum(x["status"] == "PASS" for x in self.checks),
              "fail": sum(x["status"] == "FAIL" for x in self.checks),
              "rerun": "uv run --no-sync python tests/s3_storage_e2e.py"})
        print("ARTIFACTS", self.artifacts, flush=True)


if __name__ == "__main__":
    lab = Lab()
    try:
        lab.start()
        lab.execute()
    except Exception as exc:
        lab.check("acceptance execution completed", False, category=type(exc).__name__, detail=lab.clean(str(exc)))
    finally:
        lab.close()
    raise SystemExit(1 if any(c["status"] == "FAIL" for c in lab.checks) else 0)
