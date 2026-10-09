"""New campaigns only; no implicit retries or overwriting earlier evidence."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import sys
from pathlib import Path

from inventory import MAP, ROOT, inventory, write_json
from scrapy.crawler import CrawlerProcess
from spider import UA, MapSpider, now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ids", nargs="*")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    rows = inventory()
    if args.ids:
        rows = [r for r in rows if r["id"] in args.ids]
    write_json(root / "inventory.json", rows)
    here = Path(__file__).parent
    shutil.copyfile(here / "protocol.json", root / "protocol.json")
    settings = {
        "CONCURRENT_REQUESTS": 8,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": 1.5,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "DOWNLOAD_TIMEOUT": 20,
        "DNS_TIMEOUT": 10,
        "DOWNLOAD_MAXSIZE": 5242880,
        "DOWNLOAD_WARNSIZE": 5242880,
        "DOWNLOAD_FAIL_ON_DATALOSS": True,
        "RETRY_ENABLED": False,
        "REDIRECT_ENABLED": False,
        "METAREFRESH_ENABLED": False,
        "ROBOTSTXT_OBEY": False,
        "HTTPCACHE_ENABLED": False,
        "COOKIES_ENABLED": False,
        "HTTPPROXY_ENABLED": False,
        "USER_AGENT": UA,
        "HTTPERROR_ALLOW_ALL": True,
        "TELNETCONSOLE_ENABLED": False,
        "REMOTE_CONTROL_ENABLED": False,
        "LOG_ENABLED": False,
        "DOWNLOADER_CLIENTCONTEXTFACTORY": "scrapy.core.downloader.contextfactory.BrowserLikeContextFactory",
        "DOWNLOADER_MIDDLEWARES": {"spider.Ledger": 40},
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "CLOSESPIDER_TIMEOUT": 2400,
    }
    manifest = {
        "started_at": now(),
        "command": sys.argv,
        "input_sha256": hashlib.sha256(MAP.read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "user_agent": UA,
        "packages": {
            p: importlib.metadata.version(p)
            for p in ("scrapy", "twisted", "pypdf", "lxml", "protego")
        },
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "settings": settings,
        "code_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in here.glob("*.py")
        },
        "content_scope": "No query form submission, login, CAPTCHA solving, proxy rotation or production activation",
    }
    write_json(root / "manifest.json", manifest)
    for name in ("requests.jsonl", "responses.jsonl", "errors.jsonl", "results.jsonl"):
        (root / name).touch()
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(MapSpider)
    failures = []
    deferred = process.crawl(crawler, rows=rows, root=root)
    deferred.addErrback(lambda failure: failures.append(failure.type.__name__))
    process.start()
    manifest.update(
        finished_at=now(),
        errors=failures,
        stats={
            k: v for k, v in crawler.stats.get_stats().items() if isinstance(v, (int, float, str))
        },
    )
    write_json(root / "manifest.json", manifest)
    print(json.dumps({"output": str(root), "errors": failures}, ensure_ascii=False))
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
