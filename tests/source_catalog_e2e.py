"""Independent HTTP acceptance: uv run --no-sync python tests/source_catalog_e2e.py.

Requires Docker, installed uv runtime and cached Issue22 Postgres/MinIO images.
Uses only existing independent Lab/Data fixtures; no product implementation read/import.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

import psycopg
from issue22_data import Data
from issue22_faults import ready
from issue22_lab import ROOT, Lab, digest, write
from psycopg.types.json import Jsonb

COMMAND = "uv run --no-sync python tests/source_catalog_e2e.py"


def entries(lab, ident, urls):
    config = dict(
        id=ident,
        entry_urls=urls,
        private_secret="PRIVATE_CONFIGURATION_SENTINEL",
        scope="Independent synthetic catalog fixture",
    )
    lab.sql("UPDATE seal.source SET config=%s WHERE id=%s", (Jsonb(config), ident))


def catalog(lab, expected, key=0, label="catalog matches complete authorized array"):
    status, headers, value, raw, elapsed = lab.request("/sources", key=key)
    lab.check(
        label,
        status == 200 and value == expected,
        actual_status=status,
        actual=value,
        expected=expected,
    )
    lab.check(
        "catalog strict fields and no-store",
        isinstance(value, list)
        and all(
            set(row) == {"id", "entry_urls"}
            and isinstance(row["id"], str)
            and isinstance(row["entry_urls"], list)
            and all(isinstance(u, str) for u in row["entry_urls"])
            for row in value
        )
        and headers.get("Cache-Control") == "no-store"
        and b"PRIVATE_CONFIGURATION_SENTINEL" not in raw,
    )
    return value


def scenarios(lab, data):
    unique_a = "https://fixture.example.org/unique-a"
    unique_b = "https://fixture.example.org/unique-b"
    shared = "https://fixture.example.org/shared"
    for ident in ("dd-002", "dd-003", "dd-004", "dd-005", "dd-006", "dd-007", "dd-008"):
        data.source(ident)
    entries(lab, "dd-002", [unique_b, unique_a, unique_b, shared])
    entries(lab, "dd-003", [shared])
    entries(lab, "dd-004", [shared])
    entries(lab, "dd-005", [])
    entries(lab, "dd-006", ["https://fixture.example.org/empty"])
    entries(lab, "dd-007", ["https://fixture.example.org/unauthorized"])
    # Global ambiguity from an unauthorized source must still hide the shared entry.
    entries(lab, "dd-008", [shared])
    lab.allowed[:] = ["dd-006", "dd-005", "dd-004", "dd-003", "dd-002", "dd-001", "dd-999"]
    expected = [
        dict(id="dd-001", entry_urls=[data.entries["dd-001"]]),
        dict(id="dd-002", entry_urls=[unique_b, unique_a]),
        dict(id="dd-006", entry_urls=["https://fixture.example.org/empty"]),
    ]
    lab.start_service()
    before = lab.snapshot()
    origin_before = len(lab.origin.requests)
    catalog(lab, expected)
    catalog(lab, expected[:1], key=1, label="limited caller sees only authorized source")
    for key in (None,):
        for path in ("/sources", "/sources?bad", "/sources?limit=1"):
            lab.error(path, "unauthenticated", 401, key=key)
    lab.error("/sources?bad", "unauthenticated", 401, headers={"Authorization": "Bearer invalid"})
    for query in ("bad", "limit=1", "source=x", "entry_url=x", "cursor=x", "x=", "x=1&x=2", "="):
        headers = lab.error("/sources?" + query, "invalid_parameters", 400)
        lab.check("parameter errors are no-store", headers.get("Cache-Control") == "no-store")
    listed = catalog(lab, expected)
    for item in listed if isinstance(listed, list) else expected:
        for entry in item["entry_urls"]:
            status, _, rows, _, _ = lab.request("/results?" + urlencode({"source_url": entry}))
            want = 3 if item["id"] == "dd-001" else 0
            lab.check(
                "discovered entry works through results " + item["id"],
                status == 200 and isinstance(rows, list) and len(rows) == want,
                actual_status=status,
                count=len(rows) if isinstance(rows, list) else None,
            )
            if item["id"] == "dd-001" and isinstance(rows, list):
                lab.check(
                    "discovery leads to genuine collected markdown",
                    any("authorized source publishes" in r.get("markdown", "") for r in rows),
                )
    status, _, rows, _, _ = lab.request(
        "/results?" + urlencode({"source_url": listed[0]["entry_urls"][0] + "#ignored"})
    )
    lab.check(
        "discovered URL supports normalized result requests",
        status == 200 and isinstance(rows, list) and len(rows) == 3,
    )
    lab.check(
        "catalog and selected results leave all DB/S3 data unchanged", before == lab.snapshot()
    )
    lab.check("catalog never starts origin collection", origin_before == len(lab.origin.requests))
    write(
        lab.artifacts / "readonly-snapshots.json",
        dict(
            before=before,
            after=lab.snapshot(),
            origin_before=origin_before,
            origin_after=len(lab.origin.requests),
        ),
    )
    changed = "https://fixture.example.org/changed-entry"
    entries(lab, "dd-002", [changed, changed])
    expected[1]["entry_urls"] = [changed]
    catalog(lab, expected, label="registration updates reflected with within-source deduplication")
    lab.error("/results?" + urlencode({"source_url": unique_a}), "source_not_found", 404)
    status, _, rows, _, _ = lab.request("/results?" + urlencode({"source_url": changed}))
    lab.check("changed discovered entry resolves immediately", status == 200 and rows == [])
    entries(lab, "dd-008", [changed])
    catalog(
        lab,
        [expected[0], expected[2]],
        label="unauthorized registration makes previously unique entry ambiguous",
    )
    entries(lab, "dd-008", ["https://fixture.example.org/restored-other"])
    catalog(lab, expected, label="global ambiguity removal restores source")
    lab.allowed[:] = []
    lab.start_service()
    catalog(lab, [], label="empty permission list returns []")
    lab.allowed[:] = ["dd-999"]
    lab.start_service()
    catalog(lab, [], label="authorized absent ID returns []")
    lab.allowed[:] = ["dd-001", "dd-002", "dd-006"]
    lab.start_service()
    lab.docker("stop", "--time", "1", lab.containers[1])
    try:
        catalog(lab, expected, label="discovery succeeds with object storage stopped")
    finally:
        lab.docker("start", lab.containers[1])
        ready(lab, "s3")
    lab.docker("stop", "--time", "1", lab.containers[0])
    try:
        lab.error("/sources", "database_unavailable", 503)
        lab.error("/sources?bad", "unauthenticated", 401, key=None)
    finally:
        lab.docker("start", lab.containers[0])
        ready(lab, "db")
    catalog(lab, expected, label="database recovery restores discovery")
    entries(lab, "dd-002", ["https://fixture.example.org/" + "x" * 2000])
    lab.start_service(page_max_bytes=1024)
    lab.error("/sources", "archive_too_large", 500)
    entries(lab, "dd-002", [changed])
    lab.start_service(workers=1, request_timeout_seconds=1)
    with psycopg.connect(lab.dsn) as conn:
        conn.execute("LOCK TABLE seal.source IN ACCESS EXCLUSIVE MODE")
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(lab.request, "/sources")
            time.sleep(0.2)
            others = [pool.submit(lab.request, "/sources") for _ in range(2)]
            first_result = first.result(timeout=10)
            siblings = [f.result(timeout=10) for f in others]
        lab.check(
            "blocked DB query yields bounded request timeout",
            first_result[0] == 503
            and first_result[2] == {"error": "request_timeout"}
            and first_result[4] < 3,
            response=first_result[2],
            elapsed=first_result[4],
        )
        lab.check(
            "catalog obeys shared worker capacity",
            all(r[0] == 503 and r[2] == {"error": "server_busy"} for r in siblings),
            responses=[r[2] for r in siblings],
        )
    time.sleep(0.5)
    catalog(lab, expected, label="released DB lock restores worker capacity")


def main():
    lab = Lab()
    data = Data(lab)
    try:
        lab.start()
        environment = json.loads((lab.artifacts / "environment.json").read_text())
        environment.update(
            command=COMMAND,
            catalog_failure_boundaries_sha256=digest(
                (ROOT / "tests/source_catalog_failure_boundaries.json").read_bytes()
            ),
            catalog_test_sha256=digest(Path(__file__).read_bytes()),
        )
        write(lab.artifacts / "environment.json", environment)
        write(
            lab.artifacts / "failure-boundaries.json",
            json.loads((ROOT / "tests/source_catalog_failure_boundaries.json").read_text()),
        )
        data.collect_initial()
        scenarios(lab, data)
    except Exception as exc:
        lab.check("independent catalog scenarios complete", False, exception=type(exc).__name__)
        (lab.artifacts / "failure-fatal.txt").write_text(lab.clean(traceback.format_exc()))
    finally:
        if os.environ.get("SOURCE_CATALOG_KEEP_FAILURE") and any(
            c["status"] == "FAIL" for c in lab.checks
        ):
            private = lab.work / "debug-environment.private.json"
            write(private, lab.env)
            private.chmod(0o600)
            write(
                lab.artifacts / "retained-containers.json",
                dict(containers=lab.containers, private_environment=str(private)),
            )
            lab.containers = []
        lab.close()
    text = "\n".join(p.read_text(errors="replace") for p in lab.artifacts.rglob("*") if p.is_file())
    lab.check(
        "catalog artifacts omit randomized credentials", not any(s in text for s in lab.secrets)
    )
    write(
        lab.artifacts / "summary.json",
        dict(
            command=COMMAND,
            passed=sum(c["status"] == "PASS" for c in lab.checks),
            failed=sum(c["status"] == "FAIL" for c in lab.checks),
            artifacts=str(lab.artifacts),
            test_sha256=digest(Path(__file__).read_bytes()),
            unverified=["Production deployment", "Live external websites"],
        ),
    )
    return int(any(c["status"] == "FAIL" for c in lab.checks))


if __name__ == "__main__":
    raise SystemExit(main())
