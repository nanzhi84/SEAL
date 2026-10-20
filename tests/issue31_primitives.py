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

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docs" / "reference" / "法规案例元数据上传模板.xlsx"
MINIO_IMAGE = "seal-minio:RELEASE.2025-10-15T17-29-55Z"
PG_IMAGE = "postgres:17-alpine"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def template_headers() -> list[str]:
    import openpyxl

    sheet = openpyxl.load_workbook(TEMPLATE, read_only=True).worksheets[0]
    return [str(v) for v in next(sheet.iter_rows(min_row=1, max_row=1, values_only=True))]


class NotVerified(Exception):
    """Raised when a scenario cannot be exercised in this environment."""


