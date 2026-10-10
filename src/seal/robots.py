"""Scrapy's native robots policy cache with archived, fail-closed evidence.

Policy requests use the same downloader, Guard, budgets and immutable archive.
They are auxiliary HTTP exchanges, never a second scheduler or content frontier.
"""

import asyncio
import re
from urllib.parse import urlsplit

from scrapy import Request
from scrapy.downloadermiddlewares.robotstxt import RobotsTxtMiddleware
from scrapy.exceptions import IgnoreRequest
from scrapy.http.request import NO_CALLBACK
from scrapy.utils.defer import maybe_deferred_to_future
from twisted.internet.defer import Deferred

from .archive import InputMiddleware
from .discovery import mark_failed, mark_parsed
from .seeded import robots_sitemaps


class UnavailablePolicy:
    def allowed(self, url, user_agent):
        return False


def policy_origin(url):
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def is_policy_document(body):
    """Obvious challenge documents cannot grant public crawl permission."""
    content = body.lstrip()
    return content.startswith((b"{", b"[")) or (
        content.startswith(b"<")
        and re.search(rb"<(?:!doctype|html|head|body|form|script)\b", content, re.I) is not None
    )


class ArchivedRobotsMiddleware(RobotsTxtMiddleware):
    def __init__(self, crawler):
        super().__init__(crawler)
        self.context = crawler.settings["SEAL_CONTEXT"]
        self.reasons = {}
        self.sitemaps = {}
        self.inputs = InputMiddleware.from_crawler(crawler)

    async def process_request(self, request, spider=None):
        # Only Runtime's precisely identified policy request may bypass policy.
        if not request.meta.get("_seal_robots_policy"):
            request.meta.pop("dont_obey_robotstxt", None)
        try:
            await super().process_request(request)
            request.meta["_seal_robots_sitemaps"] = self.sitemaps.get(
                policy_origin(request.url), {}
            )
        except IgnoreRequest:
            code = self.reasons.get(policy_origin(request.url), "robots_denied")
            await asyncio.to_thread(mark_failed, self.context, request, code)
            await self.inputs.fail(code)
            raise IgnoreRequest(code) from None

    async def robot_parser(self, request):
        parsed = urlsplit(request.url)
        # RFC policy belongs to an origin. The native middleware's netloc-only
        # cache could otherwise reuse an HTTP allow policy for an HTTPS deny policy.
        netloc = policy_origin(request.url)
        if netloc not in self._parsers:
            self._parsers[netloc] = Deferred()
            url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            policy = Request(
                url,
                priority=self.DOWNLOAD_PRIORITY,
                callback=NO_CALLBACK,
                meta={
                    "dont_obey_robotstxt": True,
                    "dont_redirect": True,
                    "dont_retry": True,
                    "_seal_robots_policy": True,
                    "_seal_robots_url": url,
                    "seal_role": "robots",
                    "seal_discovery_method": "robots",
                    "seal_depth": 0,
                    "seal_parent_url": request.url,
                },
            )
            try:
                # Native robots downloads bypass Scheduler, so bind the auxiliary
                # exchange to the same Discovery ledger explicitly.
                self.crawler._seal_discovery.scheduled(policy)
                response = await self.crawler.engine.download_async(policy)
                await asyncio.to_thread(self.inputs.process_spider_input, response)
                if (
                    200 <= response.status < 300 and not is_policy_document(response.body)
                ) or response.status in (404, 410):
                    if 200 <= response.status < 300:
                        self.sitemaps[netloc] = {
                            "urls": list(robots_sitemaps(response)),
                            "url": response.url,
                            "snapshot_id": response.meta["seal_snapshot_id"],
                            "observation_id": response.meta["seal_observation_id"],
                        }
                    await self._parse_robots(
                        response.replace(body=b"") if response.status in (404, 410) else response,
                        netloc,
                        request,
                    )
                    await asyncio.to_thread(mark_parsed, self.context, response.request)
                else:
                    code = (
                        "robots_denied" if response.status in (401, 403) else "robots_unavailable"
                    )
                    self.reasons[netloc] = code
                    await asyncio.to_thread(mark_failed, self.context, response.request, code)
                    self._unavailable(netloc)
            except Exception:
                self.reasons[netloc] = "robots_unavailable"
                self._unavailable(netloc)
            self._stats.inc_value("robotstxt/request_count")
        parser = self._parsers[netloc]
        if isinstance(parser, Deferred):
            return await maybe_deferred_to_future(parser)
        return parser

    def _unavailable(self, netloc):
        pending = self._parsers[netloc]
        self._parsers[netloc] = UnavailablePolicy()
        if isinstance(pending, Deferred):
            pending.callback(self._parsers[netloc])
