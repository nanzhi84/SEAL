"""Temporary Docker PostgreSQL/MinIO for independent review acceptance only."""

import json
import os
import secrets
import subprocess
import time
import urllib.request
from pathlib import Path

import psycopg

from issue25_lab import free_port


class IsolatedStorage:
    def __init__(self, work):
        self.work = Path(work)
        self.work.mkdir(parents=True, mode=0o700, exist_ok=True)
        suffix = secrets.token_hex(4)
        self.names = ["seal-review-config-pg-" + suffix, "seal-review-config-s3-" + suffix]
        self.ports = [free_port(), free_port()]
        self.created = []
        self.env = {}

    def __enter__(self):
        database_password, object_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        files = [self.work / "postgres.env.local", self.work / "minio.env.local"]
        contents = ["POSTGRES_USER=seal\nPOSTGRES_DB=seal\nPOSTGRES_PASSWORD=" + database_password + "\n",
                    "MINIO_ROOT_USER=sealreview\nMINIO_ROOT_PASSWORD=" + object_password + "\n"]
        images = ["postgres:17-alpine", "seal-minio:RELEASE.2025-10-15T17-29-55Z"]
        try:
            for index, path in enumerate(files):
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
                    stream.write(contents[index])
                path.chmod(0o600)
                command = ["docker", "run", "--detach", "--rm", "--name", self.names[index],
                           "--env-file", str(path), "-p",
                           "127.0.0.1:" + str(self.ports[index]) + (":5432" if index == 0 else ":9000")]
                command += [images[index]]
                if index:
                    command += ["server", "/data"]
                subprocess.run(command, check=True, capture_output=True, timeout=30)
                self.created.append(self.names[index])
            self.env = {
                "SEAL_DATABASE_URL": "postgresql://seal:" + database_password +
                                     "@127.0.0.1:" + str(self.ports[0]) + "/seal",
                "SEAL_S3_ENDPOINT": "http://127.0.0.1:" + str(self.ports[1]),
                "SEAL_S3_ACCESS_KEY": "sealreview", "SEAL_S3_SECRET_KEY": object_password,
                "SEAL_S3_BUCKET": "seal-review-unused-base", "SEAL_S3_REGION": "us-east-1",
                "MINIO_ROOT_USER": "sealreview", "MINIO_ROOT_PASSWORD": object_password,
            }
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                try:
                    with psycopg.connect(self.env["SEAL_DATABASE_URL"], connect_timeout=1):
                        pass
                    with urllib.request.urlopen(self.env["SEAL_S3_ENDPOINT"] + "/minio/health/live", timeout=1):
                        pass
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                raise RuntimeError("isolated storage readiness timeout")
            (self.work / "services.json").write_text(json.dumps({
                "containers": self.names, "images": images, "ports": self.ports,
                "isolation": "fresh ephemeral containers; no host MinIO paths or existing database used",
            }, indent=2))
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        for name in reversed(self.created):
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30)
