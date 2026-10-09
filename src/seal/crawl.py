"""One reactor per bounded Crawl. Ordinary lifecycle isolation, not a sandbox."""

import asyncio
import os
import signal
import sys
import threading
import time
from copy import deepcopy

from scrapy import signals
from scrapy.crawler import CrawlerProcess

from .db import connect, j, record_error
from .recipes import import_recipe
from .runs import run_context


def settings_for(context):
    config = context["config"]
    replay = context["mode"] == "replay"
    middlewares = {
        "seal.archive.RequestGuard": None if replay else 40,
        "seal.archive.ReplayMiddleware": 30 if replay else None,
        "scrapy.downloadermiddlewares.httpcompression.HttpCompressionMiddleware": None
        if replay
        else 610,
        "seal.archive.ResponseArchiveMiddleware": 605,
    }
    if replay:
        middlewares.update(
            {
                "scrapy.downloadermiddlewares.redirect.RedirectMiddleware": None,
                "scrapy.downloadermiddlewares.redirect.MetaRefreshMiddleware": None,
                "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,
            }
        )
    return {
        "SEAL_CONTEXT": context,
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "DOWNLOADER_MIDDLEWARES": middlewares,
        "SPIDER_MIDDLEWARES": {"seal.archive.InputMiddleware": 100},
        "ITEM_PIPELINES": {"seal.items.ItemPipeline": 100},
        "EXTENSIONS": {"seal.crawl.Completion": 100},
        "HTTPCACHE_ENABLED": False,
        "COOKIES_ENABLED": False,
        "HTTPPROXY_ENABLED": False,
        "ROBOTSTXT_OBEY": False,
        "RETRY_ENABLED": not replay,
        "RETRY_TIMES": 2,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408],
        "CONCURRENT_REQUESTS": config["concurrency"],
        "CONCURRENT_REQUESTS_PER_DOMAIN": config["concurrency"],
        "CONCURRENT_ITEMS": config["concurrency"],
        "DOWNLOAD_DELAY": config["delay"],
        "AUTOTHROTTLE_ENABLED": not replay,
        "AUTOTHROTTLE_START_DELAY": config["delay"],
        "AUTOTHROTTLE_MAX_DELAY": 5.0,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "DOWNLOAD_TIMEOUT": min(15, config["budget"]["seconds"]),
        "DOWNLOAD_MAXSIZE": config["budget"]["response_bytes"],
        "DOWNLOAD_WARNSIZE": config["budget"]["response_bytes"],
        "DOWNLOAD_FAIL_ON_DATALOSS": True,
        "REDIRECT_MAX_TIMES": 8,
        "CLOSESPIDER_TIMEOUT": config["budget"]["seconds"],
        "USER_AGENT": config["user_agent"],
        "TELNETCONSOLE_ENABLED": False,
        "LOG_ENABLED": False,
        "REACTOR_THREADPOOL_MAXSIZE": config["concurrency"],
        "FEEDS": {},
        "JOBDIR": None,
    }


class Completion:
    @classmethod
    def from_crawler(cls, crawler):
        value = cls()
        value.crawler = crawler
        value.context = crawler.settings["SEAL_CONTEXT"]
        crawler.signals.connect(value.closed, signal=signals.spider_closed)
        crawler.signals.connect(value.spider_error, signal=signals.spider_error)
        return value

    def spider_error(self, failure, response, spider):
        self.crawler.stats.inc_value("seal/errors")
        record_error(self.context["id"], self.context["attempt_epoch"], "spider_exception")

    async def closed(self, spider, reason):
        # Do not serialize exception messages, URLs, request headers or body into Stats.
        stats = {
            k: v
            for k, v in self.crawler.stats.get_stats().items()
            if isinstance(v, (int, float, bool))
            and k.split("/")[0]
            in {
                "downloader",
                "response_received_count",
                "item_scraped_count",
                "item_dropped_count",
                "retry",
                "dupefilter",
                "seal",
            }
        }
        classes = [
            type(m).__module__ + "." + type(m).__name__
            for m in self.crawler.engine.downloader.middleware.middlewares
        ]

        def save():
            with connect() as c:
                if stats.get("seal/errors", 0):
                    c.execute(
                        "UPDATE seal_run SET errors=%s WHERE id=%s AND attempt_epoch=%s AND errors='[]'",
                        (
                            j(["unpersisted_processing_error"]),
                            self.context["id"],
                            self.context["attempt_epoch"],
                        ),
                    )
                c.execute(
                    "UPDATE seal_run SET status='finishing',report=report || %s WHERE id=%s AND attempt_epoch=%s AND status='running'",
                    (
                        j(
                            {
                                "finish_reason": reason,
                                "stats": stats,
                                "effective_downloader_order": classes,
                                "request_budget_overshoot_upper_bound": self.context["config"][
                                    "concurrency"
                                ],
                            }
                        ),
                        self.context["id"],
                        self.context["attempt_epoch"],
                    ),
                )

        await asyncio.to_thread(save)


def watch_parent(parent):
    while True:
        if os.getppid() != parent:
            os.killpg(os.getpgrp(), signal.SIGTERM)
            time.sleep(3)
            os.killpg(os.getpgrp(), signal.SIGKILL)
            return
        time.sleep(0.5)


def main():
    run_id, epoch, parent = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    threading.Thread(target=watch_parent, args=(parent,), daemon=True).start()
    try:
        context = run_context(run_id, epoch)
        recipe = import_recipe(context["recipe_version"])
        # Recipe settings cannot silently replace archival or replay behavior.
        if recipe.custom_settings:
            raise ValueError("recipe_settings_not_supported")
        process = CrawlerProcess(settings_for(context))
        deferred = process.crawl(
            recipe, params=deepcopy(context["params"]), context=deepcopy(context)
        )
        failed = []
        deferred.addErrback(lambda failure: failed.append(type(failure.value).__name__))
        process.start()
        if failed:
            record_error(run_id, epoch, "crawl_setup_failed")
            return 1
        return 0
    except Exception as exc:
        from .core import SealError

        record_error(
            run_id, epoch, exc.code if isinstance(exc, SealError) else "crawl_process_failed"
        )
        if isinstance(exc, SealError):
            with connect() as c:
                c.execute(
                    "UPDATE seal_run SET status='finishing',report=report || %s WHERE id=%s AND attempt_epoch=%s AND status='running'",
                    (j({"finish_reason": "recipe_setup_failed"}), run_id, epoch),
                )
        return 1


if __name__ == "__main__":
    sys.exit(main())
