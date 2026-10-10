"""Native extraction and immutable strategy boundaries, without external HTTP."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from scrapy import Request
from scrapy.exceptions import IgnoreRequest
from scrapy.http import HtmlResponse

from seal import discovery, robots
from seal.core import SealError
from seal.crawl import settings_for
from seal.discovery import Discovery, DiscoverySpiderMiddleware, ResourceFingerprinter
from seal.robots import ArchivedRobotsMiddleware, is_policy_document
from seal.scope import robots_policy_request
from seal.seeded import html_links, sitemap_links

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("seal_seeded_test", ROOT / "recipes/seeded/recipe.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SeededDiscoveryTests(unittest.TestCase):
    def context(self, **kwargs):
        return {
            "mode": "collect",
            "params": {},
            "seeds": [{"url": "https://example.org/list", "role": "list"}],
            "config": {
                "robots": True,
                "output_schema": "record.v1",
                "allowed_hosts": ["example.org"],
                "allowed_path_prefixes": ["/"],
                "concurrency": 1,
                "delay": 0.0,
                "budget": {"seconds": 10, "response_bytes": 4096},
                "user_agent": "SEAL",
            },
            **kwargs,
        }

    def test_native_extraction_preserves_query_wire_spellings_and_duplicates(self):
        paths = [
            "/detail!go?q=!",
            "/detail%21go?q=%21",
            "/d?q=a+b",
            "/d?q=a%20b",
            "/d?a=1&a=2",
            "/d?a=2&a=1",
            "/d?q=%2f",
            "/d?q=%2F",
            "/d?bare&empty=&x=1&&",
        ]
        import html

        body = "".join(
            '<a href="' + html.escape(p, quote=True) + '">link</a>' for p in paths + paths[:1]
        )
        response = HtmlResponse("https://example.org/list", body=body.encode(), encoding="utf8")
        urls = [url for url, _, _ in html_links(response)]
        self.assertEqual(urls, ["https://example.org" + p for p in paths + paths[:1]])
        fingerprints = [ResourceFingerprinter().fingerprint(Request(url)) for url in urls]
        self.assertEqual(len(set(fingerprints)), len(paths))

    def test_sitemap_index_and_urlset_are_parsed_by_scrapy(self):
        for tag, entry, role in (
            ("sitemapindex", "sitemap", "sitemap"),
            ("urlset", "url", "detail"),
        ):
            body = f'<{tag} xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><{entry}><loc>https://example.org/a!b?q=a+b</loc></{entry}></{tag}>'
            response = HtmlResponse(
                "https://example.org/map.xml", body=body.encode(), encoding="utf8"
            )
            self.assertEqual(
                list(sitemap_links(response)), [("https://example.org/a!b?q=a+b", role)]
            )

    def test_policy_exception_is_exact_and_host_scoped(self):
        config = self.context()["config"]
        config["allowed_path_prefixes"] = ["/business"]
        policy = Request(
            "https://example.org/robots.txt",
            meta={
                "_seal_robots_policy": True,
                "_seal_robots_url": "https://example.org/robots.txt",
                "seal_role": "robots",
            },
        )
        self.assertTrue(robots_policy_request(config, policy))
        for url in (
            "https://example.org/business",
            "https://outside.org/robots.txt",
            "https://example.org/robots.txt?q=1",
        ):
            self.assertFalse(robots_policy_request(config, policy.replace(url=url)))

    def test_robots_sitemap_explicit_parent_keeps_policy_archive_lineage(self):
        parent = Request(
            "https://example.org/page",
            meta={"seal_snapshot_id": "html", "seal_observation_id": "html-observation"},
        )
        response = HtmlResponse(parent.url, request=parent)
        candidate = Request(
            "https://example.org/map.xml",
            meta={
                "_seal_explicit_policy_parent": True,
                "seal_parent_url": "https://example.org/robots.txt",
                "seal_parent_snapshot_id": "policy",
                "seal_parent_observation_id": "policy-observation",
            },
        )
        DiscoverySpiderMiddleware.lineage(response, candidate)
        self.assertEqual(candidate.meta["seal_parent_url"], "https://example.org/robots.txt")
        self.assertEqual(candidate.meta["seal_parent_snapshot_id"], "policy")
        self.assertEqual(candidate.meta["seal_parent_observation_id"], "policy-observation")

    def test_depth_query_and_exclusion_bounds_do_not_rewrite_urls(self):
        extension = Discovery()
        extension.context = self.context(
            seeded_policy={
                "max_depth": 2,
                "max_query_variants": 2,
                "exclude_patterns": ["*/private*"],
            }
        )
        extension.query_variants = {}
        self.assertEqual(
            extension.decision(Request("https://example.org/a", meta={"seal_depth": 3}))[0],
            "discovery_depth_exceeded",
        )
        self.assertIsNone(
            extension.decision(
                Request("https://example.org/a", meta={"seal_depth": 3}), already_seen=True
            )[0]
        )
        self.assertEqual(
            extension.decision(Request("https://example.org/private")),
            ("request_excluded", "rejected"),
        )
        for query in ("a=1&a=2", "a=2&a=1"):
            self.assertIsNone(extension.decision(Request("https://example.org/a?" + query))[0])
        self.assertEqual(
            extension.decision(Request("https://example.org/a?q=3"))[0],
            "query_variant_budget_exceeded",
        )
        self.assertIsNone(extension.decision(Request("https://example.org/a?a=1&a=2"))[0])

    def test_seeded_requires_explicit_robots_and_record_schema(self):
        context = self.context()
        context["config"]["robots"] = False
        with self.assertRaisesRegex(SealError, "seeded_robots_required"):
            MODULE.SeededSpider({}, context)
        context["config"].update(robots=True, output_schema="generic_document.v1")
        with self.assertRaisesRegex(SealError, "seeded_record_schema_required"):
            MODULE.SeededSpider({}, context)

    def test_seed_expansion_is_native_requests_and_recheck_does_not_expand(self):
        async def collect(spider):
            return [r async for r in spider.start()]

        requests = asyncio.run(collect(MODULE.SeededSpider({}, self.context())))
        self.assertEqual(
            [r.meta["seal_discovery_method"] for r in requests],
            ["seed", "homepage", "sitemap_probe"],
        )
        self.assertTrue(all(isinstance(r, Request) and not r.dont_filter for r in requests))
        requests = asyncio.run(collect(MODULE.SeededSpider({}, self.context(mode="recheck"))))
        self.assertEqual(len(requests), 1)
        replay = MODULE.SeededSpider({}, self.context(mode="replay", replay_inputs=[]))
        requests = asyncio.run(collect(replay))
        self.assertEqual(len(requests), 3)
        replay_recheck = MODULE.SeededSpider(
            {}, self.context(mode="replay", recheck_plan={"records": [{"record_id": "due"}]})
        )
        requests = asyncio.run(collect(replay_recheck))
        self.assertEqual(len(requests), 1)

    def response(self, path="/detail", body=b"", policy=None, role="detail"):
        request = Request(
            "https://example.org" + path,
            meta={
                "seal_role": role,
                "seal_snapshot_id": "page-snapshot",
                "seal_observation_id": "page-observation",
                **({"_seal_robots_sitemaps": policy} if policy else {}),
            },
        )
        return HtmlResponse(
            request.url,
            request=request,
            body=body,
            encoding="utf8",
            headers={"Content-Type": "text/html"},
        )

    def policy(self, snapshot="robots-snapshot", observation="robots-observation", origin=None):
        origin = origin or "https://example.org"
        return {
            "urls": [origin + "/map.xml", origin + "/map.xml", origin + "/second-map.xml"],
            "url": origin + "/robots.txt",
            "snapshot_id": snapshot,
            "observation_id": observation,
        }

    def test_recheck_parse_and_its_replay_do_not_explore_any_new_resource(self):
        body = (
            "<html><h1>Due document</h1><article><p>"
            + "Public text. " * 30
            + "</p></article>"
            + "".join(f'<a href="/other/{i}">Other</a>' for i in range(200))
            + '<a href="/file.pdf">PDF</a><iframe src="/frame"></iframe>'
            + '<link rel="sitemap" href="/inline-map.xml"></html>'
        ).encode()
        for context in (
            self.context(mode="recheck"),
            self.context(mode="replay", recheck_plan={"records": [{"record_id": "due"}]}),
        ):
            spider = MODULE.SeededSpider({}, context)
            spider.crawler = SimpleNamespace(stats=Mock())
            response = self.response(body=body, policy=self.policy())
            outputs = list(spider.parse(response))
            self.assertTrue(
                any(isinstance(item, dict) and item["type"] == "record" for item in outputs)
            )
            self.assertFalse(any(isinstance(item, Request) for item in outputs))
            self.assertEqual(list(spider.policy_sitemaps(response)), [])
            sitemap = self.response(
                body=b"<urlset><url><loc>https://example.org/new</loc></url></urlset>",
                role="sitemap",
            )
            self.assertEqual(list(spider.parse(sitemap)), [])

    def test_robots_sitemap_policy_version_is_discovered_once_with_first_parent(self):
        spider = MODULE.SeededSpider({}, self.context())
        first = list(spider.policy_sitemaps(self.response(policy=self.policy())))
        self.assertEqual(len(first), 2)
        for candidate in first:
            self.assertEqual(candidate.meta["seal_parent_url"], "https://example.org/robots.txt")
            self.assertEqual(candidate.meta["seal_parent_snapshot_id"], "robots-snapshot")
            self.assertEqual(candidate.meta["seal_parent_observation_id"], "robots-observation")
            self.assertEqual(
                candidate.cb_kwargs["parent"],
                {"snapshot_id": "robots-snapshot", "observation_id": "robots-observation"},
            )
        for number in range(200):
            response = self.response(
                path=f"/page/{number}", policy=self.policy(observation=f"later-{number}")
            )
            self.assertEqual(list(spider.policy_sitemaps(response)), [])
        self.assertEqual(
            len(
                list(spider.policy_sitemaps(self.response(policy=self.policy(snapshot="changed"))))
            ),
            2,
        )
        self.assertEqual(
            len(
                list(
                    spider.policy_sitemaps(
                        self.response(policy=self.policy(origin="https://other.org"))
                    )
                )
            ),
            2,
        )
        fresh_crawl = MODULE.SeededSpider({}, self.context())
        self.assertEqual(
            len(list(fresh_crawl.policy_sitemaps(self.response(policy=self.policy())))), 2
        )

    def test_robots_and_proxy_settings_keep_legacy_opt_in(self):
        context = self.context()
        context["config"]["robots"] = False
        with patch.dict("os.environ", {}, clear=True):
            settings = settings_for(context)
            self.assertFalse(settings["ROBOTSTXT_OBEY"])
            self.assertFalse(settings["HTTPPROXY_ENABLED"])
        with patch.dict("os.environ", {"SEAL_USE_ENV_PROXY": "1"}, clear=True):
            self.assertTrue(settings_for(context)["HTTPPROXY_ENABLED"])
        context["config"]["robots"] = True
        self.assertTrue(settings_for(context)["ROBOTSTXT_OBEY"])
        context["mode"] = "replay"
        self.assertLess(
            settings_for(context)["DOWNLOADER_MIDDLEWARES"]["seal.robots.ArchivedRobotsMiddleware"],
            30,
        )

    def test_robots_cache_separates_http_and_https_permission(self):
        middleware = ArchivedRobotsMiddleware.__new__(ArchivedRobotsMiddleware)
        http_policy, https_policy = object(), object()
        middleware._parsers = {
            "http://example.org": http_policy,
            "https://example.org": https_policy,
        }
        self.assertIs(
            asyncio.run(middleware.robot_parser(Request("http://example.org/a"))), http_policy
        )
        self.assertIs(
            asyncio.run(middleware.robot_parser(Request("https://example.org/a"))), https_policy
        )

    def test_policy_content_challenges_fail_closed_and_plain_policies_remain_valid(self):
        for body in (
            b"<!doctype html><form>Sign in</form>",
            b"<html>Challenge</html>",
            b'{"login":true}',
            b"\xef\xbb\xbf<html><form>Captcha</form></html>",
        ):
            self.assertTrue(is_policy_document(body))
        for body in (
            b"",
            b"User-agent: *\nDisallow: /private",
            b"# An HTML comment\nUser-agent: *",
        ):
            self.assertFalse(is_policy_document(body))

    def replay_robots(self, final_allowed=True):
        class Policy:
            def __init__(self, allowed):
                self.permission = allowed

            def allowed(self, url, user_agent):
                return self.permission

        middleware = ArchivedRobotsMiddleware.__new__(ArchivedRobotsMiddleware)
        middleware.context = self.context(mode="replay")
        middleware._parsers = {
            "http://example.org": Policy(True),
            "https://example.org": Policy(final_allowed),
        }
        middleware._robotstxt_useragent = middleware._default_useragent = "SEAL"
        middleware._stats = Mock()
        middleware.crawler = SimpleNamespace(spider=None)
        middleware.reasons = {}
        middleware.sitemaps = {
            "http://example.org": {"urls": ["http://example.org/http-map.xml"]},
            "https://example.org": {"urls": ["https://example.org/https-map.xml"]},
        }
        middleware.inputs = SimpleNamespace(fail=AsyncMock())
        return middleware

    def test_replay_redirect_rechecks_final_origin_policy_without_changing_identity(self):
        middleware = self.replay_robots()
        request = Request("http://example.org/entry", meta={"seal_discovery_id": "original"})
        fingerprint = ResourceFingerprinter().fingerprint(request)
        asyncio.run(middleware.process_request(request))
        response = HtmlResponse("https://example.org/entry", request=request)
        self.assertIs(asyncio.run(middleware.process_response(request, response)), response)
        self.assertEqual(
            request.meta["_seal_robots_sitemaps"]["urls"], ["https://example.org/https-map.xml"]
        )
        self.assertEqual(request.url, "http://example.org/entry")
        self.assertIs(response.request, request)
        self.assertEqual(ResourceFingerprinter().fingerprint(request), fingerprint)

    def test_replay_redirect_final_origin_denial_fails_original_discovery(self):
        middleware = self.replay_robots(final_allowed=False)
        request = Request("http://example.org/entry", meta={"seal_discovery_id": "original"})
        response = HtmlResponse("https://example.org/entry", request=request)
        with patch.object(robots, "mark_failed") as failed:
            with self.assertRaisesRegex(IgnoreRequest, "robots_denied"):
                asyncio.run(middleware.process_response(request, response))
            self.assertEqual(failed.call_args.args[1].meta["seal_discovery_id"], "original")
            self.assertEqual(failed.call_args.args[2], "robots_denied")
        middleware.inputs.fail.assert_awaited_once_with("robots_denied")

    def test_replay_missing_final_policy_fails_closed_without_live_fallback(self):
        middleware = self.replay_robots()
        del middleware._parsers["https://example.org"]
        middleware.crawler = SimpleNamespace(
            spider=None,
            _seal_discovery=SimpleNamespace(scheduled=Mock()),
            engine=SimpleNamespace(
                download_async=AsyncMock(side_effect=IgnoreRequest("replay_miss"))
            ),
        )
        request = Request("http://example.org/entry", meta={"seal_discovery_id": "original"})
        response = HtmlResponse("https://example.org/entry", request=request)
        with patch.object(robots, "mark_failed") as failed:
            with self.assertRaisesRegex(IgnoreRequest, "robots_unavailable"):
                asyncio.run(middleware.process_response(request, response))
            self.assertEqual(failed.call_args.args[2], "robots_unavailable")
        policy = middleware.crawler.engine.download_async.call_args.args[0]
        self.assertEqual(policy.url, "https://example.org/robots.txt")
        self.assertTrue(policy.meta["_seal_robots_policy"])
        middleware.inputs.fail.assert_awaited_once_with("robots_unavailable")

    def test_late_guard_decision_updates_metadata_only_when_migration_exists(self):
        request = Request("https://example.org/frame", meta={"seal_discovery_id": "event"})
        with patch.object(discovery, "_update") as update:
            discovery.mark_failed(
                {"_seal_discovery_metadata": True}, request, "iframe_out_of_scope"
            )
            self.assertIn("scope_decision=%s", update.call_args.args[2])
            self.assertEqual(update.call_args.args[3][-1], "rejected")
            discovery.mark_failed({}, request, "iframe_out_of_scope")
            self.assertNotIn("scope_decision", update.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
