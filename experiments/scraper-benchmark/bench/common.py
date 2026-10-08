"""Shared infrastructure for the SEAL scraper benchmark.

Provides: path constants, hashing/text-normalisation helpers, a run log with a
legacy request counters, and JSON IO. The legacy counters were post-response
and DID NOT enforce a reliable hard network budget; their live entrypoints are
now locked by network_policy.py.

Nothing here is SEAL production code. This is throwaway benchmark scaffolding.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
RESULTS = HERE / "results"
RAW = RESULTS / "raw"
PARSED = RESULTS / "parsed"
LOGS = HERE / "logs"
FIXTURES = HERE / "fixtures"
for _p in (RESULTS, RAW, PARSED, LOGS, FIXTURES):
    _p.mkdir(parents=True, exist_ok=True)

RUN_LOG_JSONL = RESULTS / "run_log.jsonl"
RUN_LOG_JSON = RESULTS / "run_log.json"
METRICS_JSON = RESULTS / "metrics.json"
PRIOR_REQUESTS = RESULTS / "prior_requests.json"

# --- budgets (hard stops) -------------------------------------------------
MAX_EXTERNAL_REQUESTS = 80
MAX_CREDITS = 30
WARN_AT = 70

USER_AGENT = "SEAL-Benchmark/0.1 (+research; contact: repository owner)"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


WS_RE = re.compile(r"[\s\u00a0\u200b\ufeff\u3000]+")


def normalize_text(text: str) -> str:
    """Canonical body text for hashing: NFKC, collapse whitespace, strip."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = WS_RE.sub(" ", t)
    return t.strip()


def canonical_content_hash(text: str) -> str:
    return sha256_text(normalize_text(text))


# Revision canonicalization lives in bench/revision.py. Do not regex-strip
# arbitrary dates/source labels: those can be legitimate legal document content.


class BudgetExceeded(RuntimeError):
    pass


class RunLog:
    """Legacy post-response accounting ONLY; not a dispatch-time safety guard."""

    def __init__(self, phase: str):
        self.phase = phase
        self._lock = threading.Lock()
        self.events: list[dict] = []
        self.external_requests = 0
        self.credits = 0
        self.prior_requests = 0
        if PRIOR_REQUESTS.exists():
            try:
                self.prior_requests = int(
                    json.loads(PRIOR_REQUESTS.read_text())["total_requests"]
                )
            except Exception:  # noqa: BLE001
                self.prior_requests = 0

    def _append(self, ev: dict) -> None:
        ev["ts"] = now_iso()
        ev["phase"] = self.phase
        with self._lock:
            self.events.append(ev)
            with RUN_LOG_JSONL.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def request(self, label: str, url: str, *, method: str = "GET", status=None,
                bytes_: int | None = None, elapsed_s: float | None = None,
                credits: int = 0, ok: bool | None = None, note: str = "",
                headers: dict | None = None) -> None:
        with self._lock:
            self.external_requests += 1
            self.credits += credits
            total = self.prior_requests + self.external_requests
            if total > MAX_EXTERNAL_REQUESTS:
                raise BudgetExceeded(
                    f"external request budget exceeded: {total} > {MAX_EXTERNAL_REQUESTS}"
                )
            if self.credits > MAX_CREDITS:
                raise BudgetExceeded(
                    f"Firecrawl credit budget exceeded: {self.credits} > {MAX_CREDITS}"
                )
        self._append({
            "kind": "request", "label": label, "method": method, "url": url,
            "status": status, "bytes": bytes_, "elapsed_s": elapsed_s,
            "credits": credits, "ok": ok, "note": note,
            "cumulative_external_requests": self.prior_requests + self.external_requests,
            "headers": headers or {},
        })

    def count_external(self, n: int, label: str, note: str = "") -> None:
        """Account for framework-internal requests (e.g. robots.txt) that the
        spider callbacks never see, so the budget stays honest."""
        with self._lock:
            self.external_requests += n
            total = self.prior_requests + self.external_requests
        self._append({"kind": "request_batch", "label": label, "count": n,
                      "note": note,
                      "cumulative_external_requests": total})

    def event(self, kind: str, label: str, **fields) -> None:
        self._append({"kind": kind, "label": label, **fields})

    def summary(self) -> dict:
        return {
            "phase": self.phase,
            "external_requests_this_run": self.external_requests,
            "prior_external_requests": self.prior_requests,
            "external_requests_total": self.prior_requests + self.external_requests,
            "firecrawl_credits_this_run": self.credits,
        }


def snapshot_path(name: str, ext: str) -> Path:
    p = RAW / f"{name}{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def save_bytes(name: str, data: bytes, ext: str) -> str:
    p = snapshot_path(name, ext)
    p.write_bytes(data)
    return str(p.relative_to(HERE))


def save_json(name: str, obj) -> str:
    p = PARSED / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(p.relative_to(HERE))


def read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, obj) -> None:
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self.t0