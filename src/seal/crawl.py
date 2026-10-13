"""Single SDK traversal; persist framework checkpoints and commit streamed results."""

import asyncio
import os
import re
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.filters import FilterChain

from .archive import serialize_result
from .core import SealError
from .db import connect, j, record_error
from .objects import Objects, work_directory
from .resources import delivered, persist, save_checkpoint
from .runs import run_context
from .sdk import (
    BrowserAdapter,
    HttpAdapter,
    InputScraper,
    RunConfig,
    ScopeFilter,
    recover_short_content,
)


class State:
    def __init__(self, context):
        self.run_id = context["id"]
        self.epoch = context["attempt_epoch"]
        self.config = context["config"]
        self.checkpoint = context["checkpoint"] or {}
        self.checkpoint.setdefault("inflight", [])
        self.count = self.checkpoint.get("requests", 0)
        self.started = time.monotonic()
        self.cancelled = False

    def save(self):
        self.checkpoint["requests"] = self.count
        save_checkpoint(self.run_id, self.epoch, self.checkpoint)

    def add_error(self, code):
        record_error(self.run_id, self.epoch, code)

    def mark_inflight(self, url):
        if url not in self.checkpoint["inflight"]:
            self.checkpoint["inflight"].append(url)
            self.save()

    def request(self, url, *, policy=False, subrequest=False):
        if self.cancelled:
            raise SealError("interrupted")
        if time.monotonic() - self.started > self.config["budget"]["seconds"]:
            raise SealError("crawl_time_budget_exceeded")
        if self.count >= self.config["budget"]["requests"]:
            raise SealError("request_budget_exceeded")
        self.count += 1
        if not policy and not subrequest:
            self.mark_inflight(url)
        self.save()

    async def checkpoint_changed(self, sdk):
        # SDK 0.9.4 omits the unfinished current level. Its visited set already contains
        # those URLs; pending must include every visited URL not durably delivered.
        done = delivered(self.run_id)
        pending = {item["url"]: item for item in sdk.get("pending", [])}
        for url in set(sdk.get("visited", [])) | set(self.checkpoint["inflight"]):
            if url not in done:
                pending.setdefault(url, {"url": url, "parent_url": None})
        sdk = dict(sdk, pending=list(pending.values()))
        self.checkpoint["sdk"] = sdk
        self.checkpoint["inflight"] = [
            url for url in self.checkpoint["inflight"] if url not in done
        ]
        self.save()

    def resume(self):
        sdk = self.checkpoint.get("sdk")
        if not sdk and not self.checkpoint["inflight"]:
            return None
        sdk = dict(sdk or {})
        pending = {item["url"]: item for item in sdk.get("pending", [])}
        # Replay an already committed but not checkpointed response to rediscover its links.
        for url in self.checkpoint["inflight"]:
            pending.setdefault(url, {"url": url, "parent_url": None})
        sdk["pending"] = list(pending.values())
        return sdk


def outcome(result, capture, config):
    status = capture.get("status") or result.status_code
    if status is not None and (status < 200 or status >= 300):
        return "http_error", "http_" + str(status)
    if capture.get("error"):
        return "parse_failed", capture["error"]
    if not capture.get("body_hash"):
        # SDK errors append source-code context, which can name other error codes.
        # Only the diagnostic prefix describes the actual exception.
        message = (result.error_message or "").split("\nCode context:", 1)[0]
        for code in (
            "robots_denied",
            "request_budget_exceeded",
            "request_out_of_scope",
            "robots_unavailable",
            "response_byte_budget_exceeded",
        ):
            if code in message:
                return "blocked", code
        return "network_failed", "download_failed"
    mime = (
        next(
            (v for k, v in capture.get("headers", {}).items() if k.lower() == "content-type"),
            "text/html",
        )
        .split(";")[0]
        .strip()
        .lower()
    )
    if mime not in ("text/html", "application/xhtml+xml", "text/plain", "application/pdf"):
        return "parse_failed", "unsupported_content_type"
    text = result.html or Objects().get(capture["body_hash"]).decode("utf-8", errors="replace")
    # SDK anti-bot detection is supplemented by narrow authentication/challenge evidence.
    blockers = (
        r'<input\b[^>]*type=["\']?password',
        r"cf-chl-|g-recaptcha|hcaptcha",
        r"<title[^>]*>[^<]*(?:sign in|log in|login|captcha|access denied|just a moment)",
        r"verify you are human|checking your browser",
    )
    if any(re.search(pattern, text, re.I) for pattern in (*blockers, *config["blocked_patterns"])):
        return "blocked", "target_content_blocked"
    if not result.success:
        if "Blocked by anti-bot" in (result.error_message or ""):
            return "blocked", "target_content_blocked"
        return "parse_failed", "parse_failed"
    mime = next(
        (v for k, v in capture.get("headers", {}).items() if k.lower() == "content-type"),
        "text/html",
    ).split(";")[0]
    if mime == "application/pdf" and not str(result.markdown or "").strip():
        return "parse_failed", "pdf_text_layer_required"
    return "parsed", None


async def run(context):
    state = State(context)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, setattr, state, "cancelled", True)
    cfg = state.config
    with tempfile.TemporaryDirectory(prefix="seal-download-", dir=work_directory()) as folder:
        adapter = (HttpAdapter if cfg["crawl4ai"]["mode"] == "http" else BrowserAdapter)(
            state, Path(folder)
        )

        async def short_content_fallback(url):
            return await recover_short_content(adapter, url)

        run_config = RunConfig(
            fallback_fetch_function=short_content_fallback,
            cache_mode=CacheMode.DISABLED,
            verbose=False,
            stream=True,
            scraping_strategy=InputScraper(adapter),
            word_count_threshold=cfg["crawl4ai"]["word_count_threshold"],
            css_selector=cfg["crawl4ai"]["css_selector"],
            page_timeout=cfg["crawl4ai"]["page_timeout_ms"],
            wait_until=cfg["crawl4ai"]["wait_until"],
            mean_delay=cfg["delay"],
            max_range=0,
            semaphore_count=cfg["concurrency"],
            max_retries=0,
        )
        async with AsyncWebCrawler(
            crawler_strategy=adapter,
            config=BrowserConfig(verbose=False),
            base_directory=folder,
        ) as crawler:

            async def consume(iterator):
                async for result in iterator:
                    capture = adapter.captures.get(result.url, {})
                    kind, error = outcome(result, capture, cfg)
                    data = serialize_result(result)
                    result_ref = Objects().put_json(data)
                    persist(
                        state.run_id,
                        state.epoch,
                        result.url,
                        capture,
                        kind,
                        error,
                        result_ref=result_ref,
                    )

            try:
                if context["mode"] == "recheck":
                    remaining = [
                        s["url"]
                        for s in context["seeds"]
                        if s["url"] not in delivered(state.run_id)
                    ]
                    if remaining:
                        await consume(await crawler.arun_many(remaining, config=run_config))
                    state.checkpoint["inflight"] = []
                    state.save()
                else:
                    start_index = state.checkpoint.get("root_index", 0)
                    for index in range(start_index, len(context["seeds"])):
                        if state.cancelled:
                            raise SealError("interrupted")
                        state.checkpoint["root_index"] = index
                        strategy = BFSDeepCrawlStrategy(
                            max_depth=cfg["crawl4ai"]["max_depth"],
                            include_external=True,
                            filter_chain=FilterChain([ScopeFilter(cfg)]),
                            resume_state=state.resume(),
                            on_state_change=state.checkpoint_changed,
                            should_cancel=lambda: state.cancelled,
                        )
                        # The SDK owns URL traversal. A network request budget bounds failures
                        # as well as successes, avoiding 0.9.4's max_pages stream off-by-one.
                        config = run_config.clone(deep_crawl_strategy=strategy)
                        await consume(
                            await crawler.arun(context["seeds"][index]["url"], config=config)
                        )
                        if state.cancelled:
                            raise SealError("interrupted")
                        state.checkpoint = {
                            "root_index": index + 1,
                            "requests": state.count,
                            "inflight": [],
                        }
                        state.save()
            except SealError as exc:
                state.add_error(exc.code)
            except Exception as exc:
                state.add_error("sdk_execution_failed:" + type(exc).__name__)
            with connect() as c:
                c.execute(
                    "UPDATE seal.run SET status='finishing',report=report || %s WHERE id=%s AND attempt_epoch=%s AND status='running'",
                    (
                        j(
                            {
                                "framework": "crawl4ai",
                                "requested": state.count,
                                "finish_reason": "interrupted" if state.cancelled else "finished",
                            }
                        ),
                        state.run_id,
                        state.epoch,
                    ),
                )


def watch_parent(parent):
    while True:
        if os.getppid() != parent:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        time.sleep(0.2)


def main():
    run_id, epoch, parent = sys.argv[1:]
    threading.Thread(target=watch_parent, args=(int(parent),), daemon=True).start()
    asyncio.run(run(run_context(run_id, int(epoch))))


if __name__ == "__main__":
    main()
