"""Minimal Scrapy spiders for the benchmark.

Deliberately small: one harvest spider that walks the declarative target plan and
runs the matching recipe, plus a Playwright variant for Test 4. No custom
framework, no persistence layer -- SEAL's own design keeps a memory frontier.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import scrapy

from . import checks, recipes
from .common import HERE, RunLog, save_bytes, save_json
from .pdfio import parse_pdf
from .targets import BY_ID

PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""

# shared across spiders in one process
RUN = RunLog("scrapy")
SEEN_RAW: dict[str, str] = {}


def _req(url: str, target_id: str, *, rendered: bool = False, **kwargs) -> scrapy.Request:
    meta = {
        "handle_httpstatus_all": True,
        "seal_target": target_id,
        "download_timeout": 45,
    }
    if PROXY:
        meta["proxy"] = PROXY
    if rendered:
        meta.update({
            "playwright": True,
            "playwright_include_page": False,
            "playwright_page_goto_kwargs": {"wait_until": "domcontentloaded"},
        })
    meta.update(kwargs.pop("meta", {}) or {})
    return scrapy.Request(url, meta=meta, **kwargs)


def should_abort_request(request):
    """Block only heavy static assets for the browser run (keeps DOM semantics)."""
    return request.resource_type in {"image", "media", "font"}


def process_response(spider, response, *, rendered: bool) -> None:
    tid = response.meta.get("seal_target") or "unknown"
    target = BY_ID.get(tid, {})
    url = response.url
    latency = response.meta.get("download_latency")
    body = response.body
    ok = response.status == 200
    RUN.request(
        tid, url, method="GET", status=response.status, bytes_=len(body),
        elapsed_s=round(latency, 3) if latency is not None else None,
        ok=ok, headers={"content-type": response.headers.get("Content-Type", b"").decode("latin-1")},
        note=("rendered" if rendered else "raw") + (" | non-200" if not ok else ""),
    )
    sha = hashlib.sha256(body).hexdigest()
    suffix = "rendered" if rendered else "raw"
    ctype = (response.headers.get("Content-Type") or b"").decode("latin-1").lower()
    ext = ".pdf" if ("pdf" in ctype or url.lower().endswith(".pdf")) else ".html"
    raw_rel = save_bytes(f"{tid}.{suffix}", body, ext)
    record = {
        "target_id": tid, "url": url, "status": response.status,
        "bytes": len(body), "sha256": sha, "raw_path": raw_rel,
        "content_type": ctype, "download_latency_s": latency,
        "rendered": rendered, "final_url": response.url,
        "redirected": bool(response.meta.get("redirect_urls")),
        "flags": list(response.flags) if hasattr(response, "flags") else [],
    }
    SEEN_RAW[f"{tid}.{suffix}"] = sha

    if not ok:
        record["outcome"] = "http_error"
        RUN.event("fetch_failed", tid, url=url, status=response.status,
                  category="access_restricted" if response.status in (401, 403, 405, 412, 429)
                  else "http_error")
        save_json(f"{tid}.{suffix}", record)
        return

    if target.get("kind") == "pdf":
        parsed = parse_pdf(body)
        record["pdf"] = {
            "page_count": parsed["page_count"],
            "extraction_seconds": parsed["extraction_seconds"],
            "error": parsed["error"],
            "parser": parsed["parser"],
        }
        save_json(f"{tid}.{suffix}.pdfparse", parsed)
        save_json(f"{tid}.{suffix}", record)
        return

    html = response.text
    recipe_name = target.get("recipe")
    if recipe_name:
        snapshot_id = f"snap_{tid}_{sha[:12]}"
        doc = recipes.RECIPES[recipe_name](response.selector, url, snapshot_id)
        doc["_meta"] = {
            "method": "scrapy" + ("+playwright" if rendered else ""),
            "recipe": recipe_name,
            "snapshot_id": snapshot_id,
            "raw_sha256": sha,
            "rendered": rendered,
        }
        doc["_checks"] = {
            "nav_leak": checks.nav_leak(doc.get("body_text", "")),
            "traceability": checks.traceability(doc.get("body_text", ""), html),
            "completeness": checks.completeness(doc.get("body_text", ""), html),
        }
        generic = __import__("bench.extract", fromlist=["extract_generic"]).extract_generic(html, url)
        doc["_generic_trafilatura"] = {
            "title": generic["title"],
            "published_at": generic["published_at"],
            "body_text": generic["body_text"],
            "error": generic["error"],
            "chars": len(generic["body_text"] or ""),
        }
        save_json(f"{tid}.{suffix}", doc)
        record["outcome"] = "parsed"
        record["parsed_path"] = f"results/parsed/{tid}.{suffix}.json"
        record["recipe"] = recipe_name

        # bounded pagination
        nxt = doc.get("next_page")
        allowed = int(target.get("follow_next_max", 0) or 0)
        if nxt and allowed > 0:
            page_no = int(response.meta.get("page_no", 1))
            if page_no <= allowed:
                child = BY_ID.get(tid)
                if child is not None:
                    new_id = f"{tid}__p{page_no + 1}"
                    BY_ID[new_id] = dict(child, id=new_id, follow_next_max=0)
                yield _req(nxt, new_id, callback=spider.parse,
                           meta={"page_no": page_no + 1},
                           dont_filter=True)
        return

    record["outcome"] = "fetched_no_recipe"
    save_json(f"{tid}.{suffix}", record)


class HarvestSpider(scrapy.Spider):
    name = "harvest"

    async def start(self):
        # Scrapy >=2.13 uses async start(); start_requests() was removed in 2.19.
        for t in self.targets:
            yield _req(t["url"], t["id"], callback=self.parse)

    def parse(self, response):
        yield from process_response(self, response, rendered=False)

    def errback(self, failure):
        tid = failure.request.meta.get("seal_target", "unknown")
        RUN.request(tid, failure.request.url, status=None, ok=False,
                    note=f"transport_error: {failure.type.__name__}")
        RUN.event("fetch_failed", tid, url=failure.request.url, status=None,
                  category="transport_error", detail=str(failure.value)[:300])


class PlaywrightHarvestSpider(scrapy.Spider):
    name = "harvest_playwright"

    async def start(self):
        for t in self.targets:
            yield _req(t["url"], t["id"], rendered=True, callback=self.parse)

    def parse(self, response):
        yield from process_response(self, response, rendered=True)

    def errback(self, failure):
        tid = failure.request.meta.get("seal_target", "unknown")
        RUN.request(tid, failure.request.url, status=None, ok=False,
                    note=f"transport_error(playwright): {failure.type.__name__}")
        RUN.event("fetch_failed", tid, url=failure.request.url, status=None,
                  category="transport_error", detail=str(failure.value)[:300])