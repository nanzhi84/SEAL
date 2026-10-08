#!/usr/bin/env python3
"""Scrapy-side benchmark entry point (Test 1-4, plus offline replay).

Usage:
  python scrapy_test.py all           # raw harvest, then Playwright harvest
  python scrapy_test.py raw           # static Scrapy only
  python scrapy_test.py playwright    # Scrapy + Playwright only (Test 4 group 3)
  python scrapy_test.py replay        # offline re-extraction from archived raw (no network)
  python scrapy_test.py pdf           # PDF parsing + table check (no network)

No new external request is made by `replay` or `pdf`.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from bench import targets
from bench.common import HERE, RAW, RESULTS, RunLog, write_json

SETTINGS_MODULE = "bench.settings"


def _crawler_process(settings: dict):
    from scrapy.crawler import CrawlerProcess
    from scrapy.settings import Settings

    from bench.settings import SETTINGS

    s = Settings()
    # NOTE: Settings.setmodule() only reads module-level UPPERCASE attributes,
    # not a dict named SETTINGS -- so the values are applied explicitly here.
    for k, v in SETTINGS.items():
        s.set(k, v, priority="project")
    for k, v in settings.items():
        s.set(k, v, priority="cmdline")
    return CrawlerProcess(s)


def run_raw() -> dict:
    from bench.network_policy import stop_legacy_network
    stop_legacy_network()
    from bench.spiders import HarvestSpider
    targets_list = [t for t in targets.TARGETS if t["id"] not in targets.PLAYWRIGHT_TARGETS]
    proc = _crawler_process({})
    crawler = proc.create_crawler(HarvestSpider)
    proc.crawl(crawler, targets=targets_list)
    proc.start()
    stats = crawler.stats.get_stats()
    return _finish("scrapy_raw", stats, targets_list)


def run_playwright() -> dict:
    from bench.network_policy import stop_legacy_network
    stop_legacy_network()
    from bench.spiders import PlaywrightHarvestSpider
    tlist = [t for t in targets.TARGETS if t["id"] in targets.PLAYWRIGHT_TARGETS]
    proc = _crawler_process({
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",
        # Use locally installed Google Chrome; the pinned Playwright browser
        # revision was not present in the machine's cache.
        "PLAYWRIGHT_LAUNCH_OPTIONS": {"headless": True, "timeout": 45_000,
                                     "channel": "chrome"},
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": 45_000,
        "PLAYWRIGHT_MAX_PAGES_PER_CONTEXT": 1,
        "PLAYWRIGHT_ABORT_REQUEST": "bench.spiders.should_abort_request",
    })
    crawler = proc.create_crawler(PlaywrightHarvestSpider)
    proc.crawl(crawler, targets=tlist)
    proc.start()
    stats = crawler.stats.get_stats()
    return _finish("scrapy_playwright", stats, tlist)


def _finish(phase: str, stats: dict, tlist: list[dict]) -> dict:
    from bench.spiders import RUN
    robots = int(stats.get("robotstxt/request_count", 0) or 0)
    if robots:
        RUN.count_external(robots, "robots_txt", note="fetched by RobotsTxtMiddleware")
    dom_ok = {t["id"]: None for t in tlist}
    summary = {
        "phase": phase,
        "budget": RUN.summary(),
        "scrapy_stats": {k: v for k, v in stats.items() if not isinstance(v, (dict,))},
        "politeness": {
            "DOWNLOAD_DELAY": 3.0,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "ROBOTSTXT_OBEY": False,
            "robots_enforced_out_of_band": True,
        },
        "targets": [t["id"] for t in tlist],
        "robots_txt_requests": robots,
        "domain_ok": dom_ok,
        "finished_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
    }
    log_path = RESULTS / f"scrapy_stats_{phase}.json"
    write_json(log_path, summary)
    print(json.dumps({"phase": phase, "robots": robots,
                      "requests": stats.get("downloader/request_count"),
                      "responses": stats.get("downloader/response_count"),
                      "statuses": {k: v for k, v in stats.items()
                                   if k.startswith("downloader/response_status_count")},
                      "budget": RUN.summary()}, ensure_ascii=False, indent=2))
    return summary


def run_replay() -> dict:
    """Preserve inherited outputs; independently repeat current extraction twice."""
    from scrapy.http import HtmlResponse
    from bench import recipes
    from bench.common import read_json, sha256_hex, sha256_text
    out = {"checked": 0, "identical_to_live": 0, "deterministic": 0,
           "changed": [], "items": {}}
    replay_dir = RESULTS / "replay"
    replay_dir.mkdir(exist_ok=True)
    plan = list(targets.TARGETS)
    p1 = read_json(RESULTS / "parsed/A_list_p1.raw.json")
    plan.append(dict(targets.BY_ID["A_list_p1"], id="A_list_p1__p2",
                     url=p1["next_page"]))
    def payload(doc):
        return {k: v for k, v in doc.items() if not k.startswith("_")}
    for t in plan:
        path = RAW / f"{t['id']}.raw.html"
        if not path.exists() or not t["recipe"]:
            continue
        raw = path.read_bytes()
        response = HtmlResponse(url=t["url"], body=raw, encoding="utf-8")
        live = read_json(RESULTS / "parsed" / f"{t['id']}.raw.json")
        snapshot = live.get("_meta", {}).get("snapshot_id", sha256_hex(raw))
        doc = recipes.apply(t["recipe"], response.text, t["url"], snapshot)
        again = recipes.apply(t["recipe"], response.text, t["url"], snapshot)
        same = payload(doc) == payload(live)
        deterministic = doc == again
        out["checked"] += 1
        out["identical_to_live"] += int(same)
        out["deterministic"] += int(deterministic)
        if not same:
            out["changed"].append(t["id"])
        out["items"][t["id"]] = {
            "recipe": t["recipe"], "identical_to_live": same,
            "deterministic": deterministic, "raw_sha256": sha256_hex(raw),
            "body_chars": len(doc.get("body_text", "")),
        }
        write_json(replay_dir / f"{t['id']}.raw.json", doc)
    write_json(RESULTS / "replay_check.json", out)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def run_pdf() -> dict:
    from bench.common import save_json, sha256_hex
    from bench.pdfio import page_for_span, parse_pdf, table_rows

    path = RAW / "C_pdf.raw.pdf"
    if not path.exists():
        print("C_pdf.raw.pdf not found; run `raw` first", file=sys.stderr)
        return {}
    data = path.read_bytes()
    parsed = parse_pdf(data)
    rows = table_rows(parsed["full_text"])
    write_json(RESULTS / "pdf_table_rows.json", rows)
    detail = {
        "raw_bytes": len(data),
        "raw_sha256": sha256_hex(data),
        "page_count": parsed["page_count"],
        "extraction_seconds": parsed["extraction_seconds"],
        "error": parsed["error"],
        "parser": parsed["parser"],
        "metadata": parsed.get("metadata"),
        "page_char_counts": [p["chars"] for p in parsed["pages"]],
        "table_rows_recovered": len(rows),
        "table_row_samples": rows[:5],
        "table_row_last": rows[-3:] if rows else [],
        "locator_example": {
            "span": [0, 60],
            "page": page_for_span(parsed["pages"], 30),
        },
    }
    save_json("C_pdf.pdfparse", parsed)
    save_json("C_pdf.check", detail)
    write_json(RESULTS / "pdf_detail.json", detail)
    print(json.dumps(detail, ensure_ascii=False, indent=2)[:2500])
    return detail


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "replay"
    if mode == "raw":
        run_raw()
    elif mode == "playwright":
        run_playwright()
    elif mode == "all":
        run_raw()
        print("--- launching Playwright group in a fresh process (reactor) ---")
        subprocess.run([sys.executable, str(HERE / "scrapy_test.py"), "playwright"], check=False)
    elif mode == "replay":
        run_replay()
    elif mode == "pdf":
        run_pdf()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))