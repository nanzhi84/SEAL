"""Crawl4AI fetching adapter: acquire bytes at the transport boundary, before decoding."""

import contextvars
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
from crawl4ai import BrowserConfig, CrawlerRunConfig, HTTPCrawlerConfig
from crawl4ai.antibot_detector import is_blocked
from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy, AsyncPlaywrightCrawlerStrategy
from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
from crawl4ai.deep_crawling.filters import URLFilter
from crawl4ai.processors.pdf import PDFContentScrapingStrategy

from .core import SealError, check_address
from .objects import Objects, work_directory
from .resources import persist
from .robots import Robots
from .scope import guard

request_context = contextvars.ContextVar("seal_request", default=None)


class RunConfig(CrawlerRunConfig):
    def clone(self, **kwargs):
        # 0.9.4 to_dict()/clone() drops fallback_fetch_function. Deep crawl clones
        # repeatedly, so retain this same-input hook at every SDK clone boundary.
        return type(self)(
            **{**self.to_dict(), "fallback_fetch_function": self.fallback_fetch_function, **kwargs}
        )


class ScopeFilter(URLFilter):
    def __init__(self, config):
        super().__init__("seal_scope")
        self.config = config

    async def apply(self, url):
        try:
            guard(self.config, url)
            return True
        except SealError:
            return False


class Resolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)
        for address in addresses:
            check_address(address["host"])
        return addresses


class Response(aiohttp.ClientResponse):
    async def read(self):
        context = request_context.get()
        if not context:
            return await super().read()
        adapter, url, policy = context
        if self._body is None:
            data = bytearray()
            async for chunk in self.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > adapter.config["budget"]["response_bytes"]:
                    adapter.capture(
                        url,
                        bytes(data[: adapter.config["budget"]["response_bytes"]]),
                        self.status,
                        str(self.url),
                        dict(self.headers),
                        error="response_byte_budget_exceeded",
                        policy=policy,
                    )
                    self.close()
                    raise SealError("response_byte_budget_exceeded")
            self._body = bytes(data)
            adapter.capture(
                url, self._body, self.status, str(self.url), dict(self.headers), policy=policy
            )
            self._release_connection()
        return self._body


class HttpAdapter(AsyncHTTPCrawlerStrategy):
    def __init__(self, state, downloads):
        self.state = state
        self.config = state.config
        self.captures = {}
        self.robots = Robots(self.config)
        super().__init__(
            HTTPCrawlerConfig(
                follow_redirects=True,
                verify_ssl=True,
                downloads_path=str(downloads),
                headers={"User-Agent": self.config["user_agent"]},
            ),
            max_connections=self.config["concurrency"],
        )

    async def start(self):
        if self._session is None:
            trace = aiohttp.TraceConfig()
            trace.on_request_redirect.append(self.redirect)
            self._session = aiohttp.ClientSession(
                response_class=Response,
                trace_configs=[trace],
                headers={"User-Agent": self.config["user_agent"]},
                connector=aiohttp.TCPConnector(
                    limit=self.config["concurrency"], resolver=Resolver()
                ),
                timeout=aiohttp.ClientTimeout(
                    total=self.config["crawl4ai"]["page_timeout_ms"] / 1000
                ),
            )

    async def redirect(self, session, context, params):
        _, _, policy = request_context.get()
        target = urljoin(str(params.response.url), params.response.headers.get("Location", ""))
        # Preserve the redirect response before an out-of-scope target is refused.
        await params.response.read()
        guard(self.config, target, policy=policy)
        self.state.request(target, policy=policy)

    def capture(self, url, body, status, actual_url, headers, *, error=None, policy=False):
        info = {
            "body_hash": Objects().put(body),
            "status": status,
            "actual_url": actual_url,
            "headers": headers,
            "input_mode": "http",
            "error": error,
        }
        self.captures[url] = info
        if policy:
            persist(self.state.run_id, self.state.epoch, url, info, "policy", role="policy")

    async def fetch_policy(self, url):
        self.state.request(url, policy=True)
        token = request_context.set((self, url, True))
        try:
            async with self._session.get(url) as response:
                return response.status, await response.read()
        finally:
            request_context.reset(token)

    async def crawl(self, url, config=None, **kwargs):
        guard(self.config, url)
        await self.start()
        await self.robots.check(url, self.fetch_policy)
        self.state.request(url)
        token = request_context.set((self, url, False))
        try:
            response = await super().crawl(url, config=config, **kwargs)
            if url in self.captures:
                self.captures[url]["html"] = response.html
            return response
        finally:
            request_context.reset(token)


class InputScraper(LXMLWebScrapingStrategy):
    """Use SDK scrapers on the exact captured input; the PDF scraper never refetches."""

    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter

    def scrap(self, url, html, **kwargs):
        capture = self.adapter.captures.get(url, {})
        headers = {k.lower(): v for k, v in capture.get("headers", {}).items()}
        mime = headers.get("content-type", "text/html").split(";")[0].strip().lower()
        if capture.get("error"):
            raise SealError(capture["error"])
        if mime == "application/pdf":
            with tempfile.TemporaryDirectory(prefix="seal-pdf-", dir=work_directory()) as folder:
                path = Path(folder) / "input.pdf"
                path.write_bytes(Objects().get(capture["body_hash"]))
                # Calling the SDK on a local immutable copy prevents its remote downloader
                # from fetching different bytes and prevents it deleting the CAS original.
                scraper = PDFContentScrapingStrategy(
                    extract_images=False,
                    max_pdf_bytes=self.adapter.config["budget"]["response_bytes"],
                )
                return scraper.scrap(str(path), "", **kwargs)
        if mime not in ("text/html", "application/xhtml+xml", "text/plain"):
            raise SealError("unsupported_content_type")
        return super().scrap(url, html, **kwargs)


class BrowserAdapter(AsyncPlaywrightCrawlerStrategy):
    """Rendered DOM mode: dedicated browser, scoped subrequests, no user browser state."""

    def __init__(self, state, downloads):
        self.state = state
        self.config = state.config
        self.captures = {}
        self.policy_transport = HttpAdapter(state, downloads)
        self.robots = self.policy_transport.robots
        super().__init__(
            BrowserConfig(
                headless=True,
                verbose=False,
                ignore_https_errors=False,
                user_agent=self.config["user_agent"],
                accept_downloads=False,
            )
        )
        self.set_hook("on_page_context_created", self.page_created)

    async def page_created(self, page, context, **kwargs):
        # Playwright routing intercepts only the first URL of a server redirect.
        # Chromium Fetch pauses EACH redirect hop before the network request.
        session = await context.new_cdp_session(page)

        async def paused(event):
            request_id = event["requestId"]
            try:
                url = event["request"]["url"]
                if not url.startswith("data:"):
                    guard(self.config, url)
                    await self.policy_transport.start()
                    await self.robots.check(url, self.policy_transport.fetch_policy)
                    self.state.request(url, subrequest=True)
                await session.send("Fetch.continueRequest", {"requestId": request_id})
            except SealError as exc:
                self.state.add_error(exc.code)
                await session.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                )

        session.on("Fetch.requestPaused", paused)
        await session.send(
            "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
        )
        return page

    async def crawl(self, url, config=None, **kwargs):
        guard(self.config, url)
        await self.policy_transport.start()
        await self.robots.check(url, self.policy_transport.fetch_policy)
        self.state.mark_inflight(url)
        response = await super().crawl(url, config=config, **kwargs)
        body = response.html.encode("utf-8")
        if len(body) > self.config["budget"]["response_bytes"]:
            raise SealError("response_byte_budget_exceeded")
        self.captures[url] = {
            "body_hash": Objects().put(body),
            "status": response.status_code,
            "actual_url": response.redirected_url or url,
            "headers": response.response_headers,
            "input_mode": "rendered_dom",
            "html": response.html,
        }
        return response

    async def close(self):
        await self.policy_transport.close()
        await super().close()


async def recover_short_content(adapter, url):
    """0.9.4 misclassifies valid short HTML as minimal_text. Reuse the SAME input,
    only for that specific size heuristic; challenge signals still fail closed.
    """
    capture = adapter.captures.get(url, {})
    html = capture.get("html", "")
    if capture.get("status") != 200 or capture.get("error") or not html:
        return ""
    blocked, reason = is_blocked(200, html)
    if not blocked or not reason.startswith(("Structural: minimal_text", "Near-empty content")):
        return ""
    if not re.search(r"<(?:p|h[1-6]|main)\b", html, re.I):
        return ""
    return html
