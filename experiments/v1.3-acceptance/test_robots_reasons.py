"""Policy availability is distinct from a robots rule denying a business URL."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from scrapy import Request
from scrapy.exceptions import IgnoreRequest
from scrapy.http import TextResponse
from twisted.internet.defer import Deferred

from seal import robots
from seal.robots import ArchivedRobotsMiddleware


class Rule:
    def __init__(self, permitted=True):
        self.permitted = permitted

    def allowed(self, url, user_agent):
        return self.permitted


class RobotsReasonTests(unittest.TestCase):
    def middleware(self, status=200, body=b"User-agent: *\nAllow: /\n", exception=None):
        value = ArchivedRobotsMiddleware.__new__(ArchivedRobotsMiddleware)
        value.context = {"mode": "collect"}
        value._parsers, value.reasons, value.sitemaps = {}, {}, {}
        value._robotstxt_useragent = value._default_useragent = "SEAL"
        value._stats = Mock()
        value.inputs = SimpleNamespace(process_spider_input=Mock(), fail=AsyncMock())

        async def downloaded(request):
            if exception:
                raise exception
            request.meta.update(seal_snapshot_id="policy", seal_observation_id="observation")
            return TextResponse(request.url, status=status, body=body, request=request)

        value.crawler = SimpleNamespace(
            spider=None,
            _seal_discovery=SimpleNamespace(scheduled=Mock()),
            engine=SimpleNamespace(download_async=AsyncMock(side_effect=downloaded)),
        )

        async def parsed(response, origin, request):
            pending = value._parsers[origin]
            value._parsers[origin] = Rule()
            if isinstance(pending, Deferred):
                pending.callback(value._parsers[origin])

        value._parse_robots = AsyncMock(side_effect=parsed)
        return value

    def assert_blocked(self, middleware, reason):
        request = Request("https://example.org/document", meta={"seal_discovery_id": "business"})
        with patch.object(robots, "mark_failed") as failed:
            with self.assertRaisesRegex(IgnoreRequest, reason):
                asyncio.run(middleware.process_request(request))
            self.assertEqual(failed.call_args.args[1], request)
            self.assertEqual(failed.call_args.args[2], reason)
        middleware.inputs.fail.assert_awaited_once_with(reason)
        return failed.call_args_list

    def test_http_401_and_403_do_not_claim_a_parsed_rule_denial(self):
        for status in (401, 403):
            with self.subTest(status=status):
                middleware = self.middleware(status=status, body=b"Domain forbidden")
                calls = self.assert_blocked(middleware, "robots_policy_http_denied")
                self.assertEqual(calls[0].args[1].meta["seal_role"], "robots")
                self.assertEqual(calls[0].args[2], "robots_policy_http_denied")
                middleware._parse_robots.assert_not_awaited()

    def test_successful_policy_rule_denial_is_robots_denied(self):
        middleware = self.middleware()
        middleware._parsers["https://example.org"] = Rule(False)
        calls = self.assert_blocked(middleware, "robots_denied")
        self.assertEqual(len(calls), 1)
        middleware.crawler.engine.download_async.assert_not_awaited()

    def test_connection_failure_terminates_the_policy_event(self):
        middleware = self.middleware(exception=ConnectionError("fixture connection unavailable"))
        calls = self.assert_blocked(middleware, "robots_unavailable")
        self.assertEqual(calls[0].args[1].meta["seal_role"], "robots")
        self.assertEqual(calls[0].args[2], "robots_unavailable")
        self.assertEqual(calls[0].kwargs, {"preserve_terminal": False})

    def test_parser_failure_terminates_archived_policy_without_exposing_sitemaps(self):
        middleware = self.middleware(body=b"User-agent: *\nSitemap: https://example.org/map.xml\n")
        middleware._parse_robots = AsyncMock(side_effect=ValueError("fixture parser failure"))
        with patch.object(robots, "mark_parsed") as parsed:
            calls = self.assert_blocked(middleware, "robots_unavailable")
            parsed.assert_not_called()
        self.assertEqual(calls[0].args[1].meta["seal_snapshot_id"], "policy")
        self.assertEqual(calls[0].kwargs, {"preserve_terminal": False})
        self.assertEqual(middleware.sitemaps, {})

    def test_guard_or_archive_failure_requests_atomic_terminal_preservation(self):
        for reason in ("request_budget_exceeded", "archive_failed", "replay_miss"):
            with self.subTest(reason=reason):
                middleware = self.middleware(exception=IgnoreRequest(reason))
                calls = self.assert_blocked(middleware, "robots_unavailable")
                self.assertEqual(calls[0].kwargs, {"preserve_terminal": True})

    def test_5xx_and_challenges_mean_unavailable_policy(self):
        for status, body in ((503, b"unavailable"), (200, b"<html>Sign in</html>")):
            with self.subTest(status=status):
                middleware = self.middleware(status=status, body=body)
                calls = self.assert_blocked(middleware, "robots_unavailable")
                self.assertEqual(calls[0].args[2], "robots_unavailable")
                middleware._parse_robots.assert_not_awaited()

    def test_absent_policy_remains_an_empty_parsed_policy(self):
        for status in (404, 410):
            with self.subTest(status=status):
                middleware = self.middleware(status=status, body=b"not found")
                with patch.object(robots, "mark_parsed") as parsed:
                    asyncio.run(middleware.process_request(Request("https://example.org/a")))
                self.assertEqual(middleware._parse_robots.call_args.args[0].body, b"")
                parsed.assert_called_once()
                middleware.inputs.fail.assert_not_awaited()

    def test_failed_policy_cache_remains_fail_closed_with_precise_reason(self):
        middleware = self.middleware(status=403)
        for _ in range(3):
            with patch.object(robots, "mark_failed"):
                with self.assertRaisesRegex(IgnoreRequest, "robots_policy_http_denied"):
                    asyncio.run(middleware.process_request(Request("https://example.org/a")))
        middleware.crawler.engine.download_async.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
