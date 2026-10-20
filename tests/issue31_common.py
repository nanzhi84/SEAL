"""Shared external-contract helpers for Issue 31 independent acceptance."""

from __future__ import annotations

import hashlib
import json
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from issue31_fixtures import (  # noqa: E402
    FIELD_BLOCK, FIELD_RULES, MIXED_NATIVE_LINES, NATIVE_PDF_PAGES, SCAN_LINES, Content, br_mixed_result,
    structure_detail,
)
from issue31_support import (  # noqa: E402
    ROOT, Api, BrOcrMock, Fixture, Lab, NotVerified, base_source, free_port, html_page, parse_json_tail,
    run_tests, sha256, template_headers,
)

HEADERS16 = template_headers()
EXTRA_KEYS = {"source_id", "resource_id", "version_id", "original_hash", "markdown_hash"}
S = {}  # shared state between ordered tests

FORMAT_FILES = {
    "page.html": "text/html; charset=utf-8",
    "note.txt": "text/plain; charset=utf-8",
    "report.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "report.doc": "application/msword",
    "plain.rtf": "application/rtf",
    "table.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "table.xls": "application/vnd.ms-excel",
    "disguised.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "disguised.doc": "application/msword",
    "bundle.zip": "application/zip",
    "native.pdf": "application/pdf",
}


# ============================================================ helpers


def current_rows(lab, source_id):
    return lab.q(
        """
        SELECT dr.*, r.id AS rid, r.external_key, r.public_url, v.sequence, v.result_ref, v.inputs_ref,
               v.change_reason AS v_reason
        FROM seal.resource r JOIN seal.frontier f ON f.id = r.frontier_id
        JOIN seal.version v ON v.id = r.current_version_id
        JOIN seal.delivery_row dr ON dr.version_id = v.id
        WHERE f.source_id = %s ORDER BY r.external_key, dr.row_number
        """,
        (source_id,),
    )


def rows_for(lab, source_id, url):
    return [r for r in current_rows(lab, source_id) if url in (r["external_key"], r["public_url"], r["detail_url"])]


def resources_for(lab, source_id, url):
    return lab.q(
        """SELECT r.* FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id
           WHERE f.source_id=%s AND (r.external_key=%s OR r.public_url=%s)""",
        (source_id, url, url),
    )


def failure_evidence(lab, source_id, url):
    """Explicit failure records touching ``url`` (observations, tasks, acquisitions)."""
    obs = lab.q(
        """SELECT o.outcome, o.error, o.version_id, o.change_reason FROM seal.observation o
           JOIN seal.resource r ON r.id=o.resource_id JOIN seal.frontier f ON f.id=r.frontier_id
           WHERE f.source_id=%s AND (r.external_key=%s OR r.public_url=%s) ORDER BY o.observed_at""",
        (source_id, url, url),
    )
    tasks = lab.q(
        """SELECT t.kind, t.state, t.error, t.key, t.payload FROM seal.task t JOIN seal.run run ON run.id=t.run_id
           WHERE run.source_id=%s AND (t.payload::text LIKE %s OR t.key LIKE %s) ORDER BY t.created_at""",
        (source_id, f"%{url}%", f"%{url}%"),
    )
    acq = lab.q(
        "SELECT state, outcome, http_status, error, body_hash FROM seal.acquisition WHERE source_id=%s AND request_url=%s",
        (source_id, url),
    )
    return {"observations": obs, "tasks": [{k: v for k, v in t.items() if k != "payload"} for t in tasks], "acquisitions": acq}


def md(lab, digest):
    return lab.s3_get(digest).decode("utf-8")


def doc_json(lab, ref):
    return json.loads(lab.s3_get(ref))


def ext_of(name):
    return name.rsplit(".", 1)[-1].lower() if name and "." in name else ""


def single_row(lab, source_id, url):
    rows = rows_for(lab, source_id, url)
    lab.check(f"exactly one delivery row for {url}", len(rows) == 1,
              {"rows": [{k: r[k] for k in ("title", "markdown_file", "original_file", "volume_order")} for r in rows],
               "failure": failure_evidence(lab, source_id, url) if not rows else None})
    return rows[0]


def check_file_row(lab, row, fixture_bytes, text_parts, ext=None, label=""):
    lab.check(f"{label} 文件顺序 empty for single file", row["volume_order"] is None, row["volume_order"])
    lab.check(f"{label} original_hash equals SHA-256 of served original", row["original_hash"] == sha256(fixture_bytes),
              {"original_hash": row["original_hash"], "expected": sha256(fixture_bytes)})
    cas = lab.s3_get(row["original_hash"])
    lab.check(f"{label} original CAS bytes identical to served bytes", cas == fixture_bytes, len(cas))
    body = md(lab, row["markdown_hash"])
    lab.check(f"{label} markdown CAS object hashes to markdown_hash",
              sha256(body.encode()) == row["markdown_hash"], row["markdown_hash"])
    missing = [p for p in text_parts if p not in body]
    lab.check(f"{label} markdown keeps source text", not missing, {"missing": missing, "markdown": body[:1500]})
    title = row["title"]
    lab.check(f"{label} 解析后文件 follows <资料名称>_<hash8>.md", row["markdown_file"] == f"{title}_{row['markdown_hash'][:8]}.md",
              row["markdown_file"])
    if ext:
        lab.check(f"{label} 原始文件 named <资料名称>_<hash8>.{ext}", row["original_file"] == f"{title}_{row['original_hash'][:8]}.{ext}",
                  row["original_file"])
    return body


def collect(lab, source_id, config, label=None, timeout=900, env_extra=None):
    path = lab.source_file(source_id, config)
    result, receipt = lab.cli_json("collect", path, "--no-progress", timeout=timeout, label=label or source_id, env_extra=env_extra)
    return result, receipt, path


def counts(receipt):
    try:
        return receipt["report"]["counts"]
    except Exception:  # noqa: BLE001
        return {}


