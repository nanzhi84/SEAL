"""Scrapy settings for the benchmark.

Politeness is enforced here (>=3s per-domain delay, 1 concurrent request per
domain). This file preserves the historical live configuration; live entrypoints
are now locked. Cached robots checks were incomplete (notably D), so these
settings alone are NOT a compliant reusable production fetcher.
"""
import os

from .common import USER_AGENT

SETTINGS = {
    "BOT_NAME": "sealbench",
    "USER_AGENT": USER_AGENT,
    # Historical setting, NOT approval: D robots failed; A/C/H returned HTML.
    # A new live campaign must perform a fresh, fail-closed policy review.
    "ROBOTSTXT_OBEY": False,

    # politeness / budget
    "CONCURRENT_REQUESTS": 4,
    "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
    "DOWNLOAD_DELAY": 3.0,
    "DOWNLOAD_DELAY_JITTER": 0,
    "DOWNLOAD_TIMEOUT": 40,
    "DOWNLOAD_MAXSIZE": 10_000_000,
    "DOWNLOAD_WARNSIZE": 0,

    # no redirect surprises beyond a bounded chain
    "REDIRECT_MAX_TIMES": 5,
    "REDIRECT_ENABLED": False,

    # bounded retries (RetryMiddleware only retries configured codes)
    "RETRY_ENABLED": False,
    "RETRY_TIMES": 1,
    "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429],

    "COOKIES_ENABLED": False,
    "TELNETCONSOLE_ENABLED": False,
    "REMOTE_CONTROL_ENABLED": False,
    "LOG_LEVEL": "INFO",
    "LOGSTATS_INTERVAL": 0,
    "TELNETCONSOLE_PORT": None,
    "FEEDS": {},
    "DNS_TIMEOUT": 20,
}

# Historical explicit proxy setting. Scrapy HttpProxyMiddleware also supports
# ambient proxies. A local outbound proxy does NOT make Firecrawl Cloud use the
# same egress IP or route to target sites; no fair network-path claim is made.
_AMBIENT_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
SETTINGS["SEALBENCH_PROXY"] = _AMBIENT_PROXY

PLAYWRIGHT_SETTINGS = {
    "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
    "DOWNLOAD_HANDLERS": {
        "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    },
    "PLAYWRIGHT_BROWSER_TYPE": "chromium",
    "PLAYWRIGHT_LAUNCH_OPTIONS": {"headless": True, "timeout": 45_000, "channel": "chrome"},
    "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": 45_000,
    "PLAYWRIGHT_MAX_PAGES_PER_CONTEXT": 1,
    "PLAYWRIGHT_ABORT_REQUEST": "bench.spiders.should_abort_request",
}