#!/usr/bin/env python3
"""Probe entry URLs with GET + a realistic browser UA.

Purpose: separate genuine access restrictions (WAF/403/412) from artifacts of
HEAD or an unusual User-Agent. Saves raw bytes to results/raw/probe_*.bin and
records status/headers/size. Read-only; no access-control bypass.
"""
from __future__ import annotations

import hashlib
import json
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
RAW = RESULTS / "raw"
RAW.mkdir(parents=True, exist_ok=True)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
PLAIN_UA = "SEAL-Benchmark/0.1 (+research)"

TARGETS = {
    "A_entry": "https://www.court.gov.cn/shenpan/gengduo/77.html",
    "B_entry": "https://www.ccgp.gov.cn/",
    "C_pdf": "https://www.nppa.gov.cn/bsfw/cyjghcpcx/202112/P020211208753495011054.pdf",
    "D_entry": "https://find-and-update.company-information.service.gov.uk/",
    "E_entry": "https://www.stats.gov.cn/xxgk/list4.html",
    "F_entry": "https://www.creditchina.gov.cn/",
    "G_entry": "https://www.nmpa.gov.cn/datasearch/home-index.html",
    "H_entry": "https://www.spp.gov.cn/spp/jczdal/index.shtml",
}
CTX = ssl.create_default_context()
MAXB = 6_000_000


def get(url: str, ua: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            body = r.read(MAXB)
            return {"status": r.status, "final_url": r.geturl(),
                    "content_type": r.headers.get("Content-Type"),
                    "body": body, "elapsed_s": round(time.time() - t0, 3)}
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(2000)
        except Exception:  # noqa: BLE001
            pass
        return {"status": e.code, "final_url": url,
                "content_type": (e.headers or {}).get("Content-Type"),
                "body": body, "elapsed_s": round(time.time() - t0, 3),
                "error": f"HTTPError {e.code} {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "final_url": url, "content_type": None,
                "body": b"", "elapsed_s": round(time.time() - t0, 3),
                "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    out = {"started_at": datetime.now(timezone.utc).isoformat(),
           "browser_ua": BROWSER_UA, "results": {}, "request_count": 0}
    for tid, url in TARGETS.items():
        rec = {"url": url}
        r = get(url, BROWSER_UA)
        out["request_count"] += 1
        ext = ".pdf" if (r["content_type"] or "").lower().startswith("application/pdf") else ".html"
        path = RAW / f"probe_{tid}{ext}"
        path.write_bytes(r["body"])
        rec["browser_ua"] = {
            "status": r["status"], "final_url": r["final_url"],
            "content_type": r["content_type"], "bytes": len(r["body"]),
            "sha256": hashlib.sha256(r["body"]).hexdigest()[:24],
            "elapsed_s": r["elapsed_s"], "error": r.get("error"),
            "saved": str(path.relative_to(HERE)),
            "head": r["body"][:300].decode("utf-8", "replace") if ext == ".html" else "",
        }
        # Distinguish UA-driven blocking for the suspicious ones.
        if r["status"] in (403, 412, 429, None):
            r2 = get(url, PLAIN_UA)
            out["request_count"] += 1
            rec["plain_ua"] = {"status": r2["status"], "error": r2.get("error"),
                               "head": r2["body"][:200].decode("utf-8", "replace")}
        out["results"][tid] = rec
        print(f"[{tid}] browserUA={r['status']} bytes={len(r['body'])} ct={r['content_type']}"
              + (f" | plainUA={rec.get('plain_ua',{}).get('status')}" if 'plain_ua' in rec else ""),
              flush=True)
        time.sleep(3.0)
    out["finished_at"] = datetime.now(timezone.utc).isoformat()
    (RESULTS / "probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("requests:", out["request_count"])


if __name__ == "__main__":
    from bench.network_policy import stop_legacy_network
    stop_legacy_network()
    main()