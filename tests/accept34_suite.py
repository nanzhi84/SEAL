#!/usr/bin/env python3
"""Run bounded API-only issue #34 real-CLI acceptances and aggregate evidence."""

import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
ART = ROOT / "artifacts" / "accept34" / ("suite-" + STAMP)
ART.mkdir(parents=True)
START = time.monotonic()


def hashes(folder, pattern):
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.glob(pattern))
        if p.is_file() and (folder.name != "src" or p.suffix in (".py", ".sql"))
    }


SOURCE = hashes(ROOT / "src", "**/*")
TESTS = hashes(ROOT / "tests", "accept34*")
ENV = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
CASES = [
    ("inline", ["accept34_e2e.py", "--fixture", "inline"]),
    ("metadata", ["accept34_e2e.py", "--fixture", "metadata"]),
    ("recovery", ["accept34_recovery.py"]),
    ("failures", ["accept34_failures.py"]),
    ("conflicts", ["accept34_conflicts.py"]),
    ("nondeterminism", ["accept34_nondeterminism.py"]),
    ("empty-recheck", ["accept34_empty_recheck.py"]),
]
HEAD = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
(ART / "prerequisites.json").write_text(
    json.dumps(
        {
            "head": HEAD,
            "python": sys.executable,
            "requires": [
                "source /Users/yoryon/SEAL/work/dev/env.sh",
                "PostgreSQL CREATE DATABASE permission",
                "S3 bucket creation/API access",
                "tesseract + eng model",
                "LibreOffice (manifest detection)",
                "Pillow Arial font available on macOS",
            ],
            "scope": "API-only; all cancelled issue32 capability excluded from execution and outcome counts",
            "isolation": "Every case creates a new database/bucket; credentials only in private work environment.json, never copied to artifacts",
            "source_start_hashes": SOURCE,
            "tests_start_hashes": TESTS,
        },
        indent=2,
    )
    + "\n"
)


def run(case):
    name, args = case
    start = time.monotonic()
    p = subprocess.run(
        [sys.executable, str(ROOT / "tests" / args[0]), *args[1:]],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        timeout=300,
    )
    (ART / (name + ".log")).write_text(p.stdout + p.stderr)
    m = re.findall(r"^ARTIFACT (.+)$", p.stdout, re.M)
    path = Path(m[-1]) if m else None
    assertions = (
        json.loads((path / "assertions.json").read_text())
        if path and (path / "assertions.json").exists()
        else []
    )
    row = {
        "case": name,
        "command": ["python", str(ROOT / "tests" / args[0]), *args[1:]],
        "returncode": p.returncode,
        "seconds": round(time.monotonic() - start, 2),
        "artifact": str(path) if path else None,
        "assertions": len(assertions),
        "failed": [a for a in assertions if not a["passed"]],
    }
    print(
        json.dumps({k: v for k, v in row.items() if k != "failed"}, ensure_ascii=False), flush=True
    )
    return row


rows = []
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    futures = {pool.submit(run, c): c[0] for c in CASES}
    for future in concurrent.futures.as_completed(futures):
        try:
            rows.append(future.result())
        except Exception as exc:
            rows.append(
                {
                    "case": futures[future],
                    "returncode": -1,
                    "error": str(exc),
                    "failed": [{"assertion": "case finished", "passed": False}],
                }
            )
        (ART / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
summary = {
    "start_head": HEAD,
    "end_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    "seconds": round(time.monotonic() - START, 2),
    "source_frozen": SOURCE == hashes(ROOT / "src", "**/*"),
    "tests_frozen": TESTS == hashes(ROOT / "tests", "accept34*"),
    "cases": rows,
    "total_assertions": sum(r.get("assertions", 0) for r in rows),
    "failures": sum(len(r["failed"]) for r in rows),
    "suite_artifact": str(ART),
}
(ART / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print("SUITE_ARTIFACT", ART, flush=True)
sys.exit(
    any(r["returncode"] or r["failed"] for r in rows)
    or not summary["source_frozen"]
    or not summary["tests_frozen"]
)
