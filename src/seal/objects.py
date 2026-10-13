"""Private S3 CAS; relational references commit only after verified object reads."""

import atexit
import json
import os
import re
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .core import SealError, atomic_write, canonical, digest


@cache
def client(endpoint, access_key, secret_key, region):
    value = boto3.session.Session().client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=10,
            retries={"mode": "standard", "total_max_attempts": 2},
        ),
    )
    atexit.register(value.close)
    return value


def work_directory():
    path = Path(os.environ.get("SEAL_WORK_DIR", "work/seal")).resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class Objects:
    def __init__(self):
        if os.environ.get("SEAL_ARCHIVE"):
            raise SealError("SEAL_ARCHIVE_removed_use_SEAL_S3_configuration")
        settings = {}
        for name in ("ENDPOINT", "BUCKET", "ACCESS_KEY", "SECRET_KEY"):
            value = os.environ.get("SEAL_S3_" + name)
            if not value:
                raise SealError("SEAL_S3_" + name + "_required")
            settings[name] = value
        try:
            endpoint = urlsplit(settings["ENDPOINT"])
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or endpoint.path not in {"", "/"}
            ):
                raise ValueError
            _ = endpoint.port
        except ValueError:
            raise SealError("invalid_object_storage_endpoint") from None
        self.bucket = settings["BUCKET"]
        self.client = client(
            settings["ENDPOINT"],
            settings["ACCESS_KEY"],
            settings["SECRET_KEY"],
            os.environ.get("SEAL_S3_REGION", "us-east-1"),
        )

    def check(self):
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError, OSError):
            raise SealError("object_storage_unavailable") from None

    @staticmethod
    def key(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise SealError("invalid_object_key")
        return value

    def put(self, body):
        key = digest(body)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/octet-stream",
                IfNoneMatch="*",
            )
        except ClientError as exc:
            # A concurrent writer or a retried successful PUT may already own the key.
            # Never overwrite it: re-read and verify the original bytes instead.
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise SealError("archive_write_failed") from None
            self.get(key)
        except (BotoCoreError, OSError):
            raise SealError("archive_write_failed") from None
        return key

    def get(self, key):
        self.key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            stream = response["Body"]
            try:
                body = stream.read()
            finally:
                stream.close()
        except (BotoCoreError, ClientError, OSError):
            raise SealError("archive_unreadable") from None
        if digest(body) != key:
            raise SealError("archive_corrupt")
        return body

    def materialize(self, key, directory=None):
        body = self.get(key)
        root = Path(directory) if directory is not None else work_directory() / "downloads"
        path = root / key
        atomic_write(path, body)
        return path.resolve()

    def put_json(self, value):
        return self.put(canonical(value))

    def json(self, key):
        return json.loads(self.get(key))
