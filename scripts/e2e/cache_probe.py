"""Native HttpCache PoC only. Never imports SEAL publication paths."""

import json
import sys
from pathlib import Path

import scrapy
from scrapy.crawler import CrawlerProcess

events = []


class BeforeCache:
    def process_response(self, request, response):
        events.append(
            {
                "stage": "before_cache_response",
                "status": response.status,
                "origin": "cache_use" if "cached" in response.flags else "network",
            }
        )
        return response

    def process_exception(self, request, exception):
        events.append({"stage": "before_cache_exception", "error": type(exception).__name__})


class AfterCache:
    def process_response(self, request, response):
        events.append(
            {
                "stage": "application_response",
                "status": response.status,
                "cached": "cached" in response.flags,
                "body": response.body.decode(),
            }
        )
        return response


class Probe(scrapy.Spider):
    name = "cache_probe"

    async def start(self):
        yield scrapy.Request(
            sys.argv[1],
            callback=self.parse,
            meta={"dont_cache": sys.argv[2] == "dont_cache"},
            dont_filter=True,
        )

    def parse(self, response):
        if sys.argv[2] in ("rfc", "fallback") and not response.meta.get("second"):
            yield scrapy.Request(
                sys.argv[1], callback=self.parse, meta={"second": True}, dont_filter=True
            )


if __name__ == "__main__":
    mode = sys.argv[2]
    process = CrawlerProcess(
        {
            "LOG_ENABLED": False,
            "ROBOTSTXT_OBEY": False,
            "TELNETCONSOLE_ENABLED": False,
            "HTTPPROXY_ENABLED": False,
            "HTTPCACHE_ENABLED": True,
            "HTTPCACHE_DIR": sys.argv[3],
            "HTTPCACHE_POLICY": "scrapy.extensions.httpcache.RFC2616Policy"
            if mode in ("rfc", "fallback")
            else "scrapy.extensions.httpcache.DummyPolicy",
            "HTTPCACHE_IGNORE_HTTP_CODES": [500] if mode == "ignore" else [],
            "RETRY_TIMES": 2 if mode not in ("rfc", "fallback") else 0,
            "HTTPERROR_ALLOW_ALL": True,
            "DOWNLOADER_MIDDLEWARES": {"__main__.BeforeCache": 901, "__main__.AfterCache": 605},
            "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        }
    )
    process.crawl(Probe)
    process.start()
    Path(sys.argv[4]).write_text(json.dumps(events, indent=2))
