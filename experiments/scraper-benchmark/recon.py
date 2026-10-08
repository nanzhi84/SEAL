#!/usr/bin/env python3
"""Reconnaissance: robots.txt + reachability for candidate sources.

Read-only probing (HEAD/GET of robots.txt). Does NOT bypass any access control.
Writes results/recon.json and appends to the shared run log.
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

UA = "SEAL-Benchmark/0.1 (+research; contact: repo owner)"

SOURCES = {
    "A_court": {
        "name": "最高人民法院指导案例",
        "entry": "https://www.court.gov.cn/shenpan/gengduo/77.html",
        "origin": "https://www.court.gov.cn",
    },
    "B_ccgp": {
        "name": "中国政府采购网",
        "entry": "https://www.ccgp.gov.cn/",
        "origin": "https://www.ccgp.gov.cn",
    },
    "C_nppa_pdf": {
        "name": "国家新闻出版署 PDF",
        "entry": "https://www.nppa.gov.cn/bsfw/cyjghcpcx/202112/P020211208753495011054.pdf",
        "origin": "https://www.nppa.gov.cn",
    },
    "D_companies_house": {
        "name": "UK Companies House",
        "entry": "https://find-and-update.company-information.service.gov.uk/",
        "origin": "https://find-and-update.company-information.service.gov.uk",
    },
    "E_stats": {
        "name": "国家统计局",
        "entry": "https://www.stats.gov.cn/xxgk/list4.html",
        "origin": "https://www.stats.gov.cn",
    },
    "F_creditchina": {
        "name": "信用中国",
        "entry": "https://www.creditchina.gov.cn/",
        "origin": "https://www.creditchina.gov.cn",
    },
    "G_nmpa": {
        "name": "国家药监局数据查询",
        "entry": "https://www.nmpa.gov.cn/datasearch/home-index.html",
        "origin": "https://www.nmpa.gov.cn",
    },
    "H_spp": {
        "name": "最高检指导案例",
        "entry": "https://www.spp.gov.cn/spp/jczdal/index.shtml",
        "origin": "https://www.spp.gov.cn",
    },
}

CTX = ssl.create_default_context()


def probe(url: str, method: str = "GET", limit: int = 200_000) -> dict:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
            body = resp.read(limit) if method == "GET" else b""
            return {
                "ok": True,
                "status": resp.status,
                "final_url": resp.geturl(),
                "headers": dict(resp.headers),
                "bytes": len(body),
                "elapsed_s": round(time.time() - t0, 3),
                "body": body,
            }
    except urllib.error.HTTPError as e:
        return {
            "ok": False, "status": e.code, "final_url": url,
            "headers": dict(e.headers or {}), "bytes": 0,
            "elapsed_s": round(time.time() - t0, 3),
            "error": f"HTTPError {e.code} {e.reason}", "body": b"",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False, "status": None, "final_url": url, "headers": {},
            "bytes": 0, "elapsed_s": round(time.time() - t0, 3),
            "error": f"{type(e).__name__}: {e}", "body": b"",
        }


def parse_robots(txt: str, ua: str = UA) -> dict:
    """Very small robots parser: returns rules for '*' plus our UA group."""
    groups: dict[str, list[tuple[str, str]]] = {}
    cur: list[str] = []
    for raw in txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            cur = [v.lower()]
            groups.setdefault(v.lower(), [])
        elif k in ("disallow", "allow") and cur:
            for agent in cur:
                groups.setdefault(agent, []).append((k, v))
    return {
        "rules_star": groups.get("*", []),
        "rules_self": groups.get(ua.lower(), []),
        "groups": sorted(groups.keys()),
    }


def main() -> int:
    out: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "user_agent": UA,
        "sources": {},
        "request_count": 0,
    }
    for sid, meta in SOURCES.items():
        rec: dict = {"name": meta["name"], "entry_url": meta["entry"]}
        rb = probe(meta["origin"] + "/robots.txt", limit=100_000)
        out["request_count"] += 1
        rec["robots"] = {
            "status": rb.get("status"),
            "error": rb.get("error"),
            "final_url": rb.get("final_url"),
        }
        if rb.get("body"):
            txt = rb["body"].decode("utf-8", "replace")
            rec["robots"].update(parse_robots(txt))
            rec["robots"]["text_sha256_prefix"] = __import__("hashlib").sha256(
                rb["body"]
            ).hexdigest()[:16]
            rec["robots"]["text_head"] = txt[:1500]
        else:
            rec["robots"]["text_head"] = rb.get("error") or "empty"

        # Save robots raw
        (RESULTS / "raw").mkdir(parents=True, exist_ok=True)
        (RESULTS / "raw" / f"robots_{sid}.txt").write_bytes(rb.get("body") or b"")

        time.sleep(3.0)  # politeness between same-ish hosts; be conservative
        en = probe(meta["entry"], method="HEAD")
        out["request_count"] += 1
        rec["entry"] = {
            "status": en.get("status"),
            "final_url": en.get("final_url"),
            "content_type": en.get("headers", {}).get("Content-Type"),
            "content_length": en.get("headers", {}).get("Content-Length"),
            "error": en.get("error"),
            "elapsed_s": en.get("elapsed_s"),
        }
        out["sources"][sid] = rec
        print(f"[{sid}] robots={rec['robots']['status']} entry={rec['entry']['status']} "
              f"ct={rec['entry']['content_type']}", flush=True)
        time.sleep(3.0)

    out["finished_at"] = datetime.now(timezone.utc).isoformat()
    (RESULTS / "recon.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"total external requests: {out['request_count']}")
    return 0


if __name__ == "__main__":
    from bench.network_policy import stop_legacy_network
    stop_legacy_network()
    sys.exit(main())