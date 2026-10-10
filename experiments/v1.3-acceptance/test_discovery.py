"""Native extraction and immutable strategy boundaries, without external HTTP."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from scrapy import Request
from scrapy.http import HtmlResponse

from seal.core import SealError
from seal.crawl import settings_for
from seal.discovery import Discovery, DiscoverySpiderMiddleware, ResourceFingerprinter
from seal.scope import robots_policy_request
from seal.robots import ArchivedRobotsMiddleware, is_policy_document
from seal.seeded import html_links, sitemap_links

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("seal_seeded_test", ROOT / "recipes/seeded/recipe.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SeededDiscoveryTests(unittest.TestCase):
    def context(self, **kwargs):
        return {
            "mode": "collect", "params": {}, "seeds": [{"url": "https://example.org/list", "role": "list"}],
            "config": {"robots": True, "output_schema": "record.v1", "allowed_hosts": ["example.org"],
                       "allowed_path_prefixes": ["/"], "concurrency": 1, "delay": 0.0,
                       "budget": {"seconds": 10, "response_bytes": 4096}, "user_agent": "SEAL"},
            **kwargs,
        }

    def test_native_extraction_preserves_query_wire_spellings_and_duplicates(self):
        paths = ["/detail!go?q=!", "/detail%21go?q=%21", "/d?q=a+b", "/d?q=a%20b",
                 "/d?a=1&a=2", "/d?a=2&a=1", "/d?q=%2f", "/d?q=%2F", "/d?bare&empty=&x=1&&"]
        import html

        body = "".join('<a href="' + html.escape(p, quote=True) + '">link</a>' for p in paths + paths[:1])
        response = HtmlResponse("https://example.org/list", body=body.encode(), encoding="utf8")
        urls = [url for url, _, _ in html_links(response)]
        self.assertEqual(urls, ["https://example.org" + p for p in paths + paths[:1]])
        fingerprints = [ResourceFingerprinter().fingerprint(Request(url)) for url in urls]
        self.assertEqual(len(set(fingerprints)), len(paths))

    def test_sitemap_index_and_urlset_are_parsed_by_scrapy(self):
        for tag, entry, role in (("sitemapindex", "sitemap", "sitemap"), ("urlset", "url", "detail")):
            body = f'<{tag} xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><{entry}><loc>https://example.org/a!b?q=a+b</loc></{entry}></{tag}>'
            response = HtmlResponse("https://example.org/map.xml", body=body.encode(), encoding="utf8")
            self.assertEqual(list(sitemap_links(response)), [("https://example.org/a!b?q=a+b", role)])

    def test_policy_exception_is_exact_and_host_scoped(self):
        config = self.context()["config"]
        config["allowed_path_prefixes"] = ["/business"]
        policy = Request("https://example.org/robots.txt", meta={
            "_seal_robots_policy": True, "_seal_robots_url": "https://example.org/robots.txt", "seal_role": "robots"
        })
        self.assertTrue(robots_policy_request(config, policy))
        for url in ("https://example.org/business", "https://outside.org/robots.txt", "https://example.org/robots.txt?q=1"):
            self.assertFalse(robots_policy_request(config, policy.replace(url=url)))

    def test_robots_sitemap_explicit_parent_keeps_policy_archive_lineage(self):
        parent = Request("https://example.org/page", meta={"seal_snapshot_id": "html", "seal_observation_id": "html-observation"})
        response = HtmlResponse(parent.url, request=parent)
        candidate = Request("https://example.org/map.xml", meta={
            "_seal_explicit_policy_parent": True, "seal_parent_url": "https://example.org/robots.txt",
            "seal_parent_snapshot_id": "policy", "seal_parent_observation_id": "policy-observation",
        })
        DiscoverySpiderMiddleware.lineage(response, candidate)
        self.assertEqual(candidate.meta["seal_parent_url"], "https://example.org/robots.txt")
        self.assertEqual(candidate.meta["seal_parent_snapshot_id"], "policy")
        self.assertEqual(candidate.meta["seal_parent_observation_id"], "policy-observation")

    def test_depth_query_and_exclusion_bounds_do_not_rewrite_urls(self):
        extension = Discovery()
        extension.context = self.context(seeded_policy={"max_depth": 2, "max_query_variants": 2, "exclude_patterns": ["*/private*"]})
        extension.query_variants = {}
        self.assertEqual(extension.decision(Request("https://example.org/a", meta={"seal_depth": 3}))[0], "discovery_depth_exceeded")
        self.assertIsNone(extension.decision(Request("https://example.org/a", meta={"seal_depth": 3}), already_seen=True)[0])
        self.assertEqual(extension.decision(Request("https://example.org/private")), ("request_excluded", "rejected"))
        for query in ("a=1&a=2", "a=2&a=1"):
            self.assertIsNone(extension.decision(Request("https://example.org/a?" + query))[0])
        self.assertEqual(extension.decision(Request("https://example.org/a?q=3"))[0], "query_variant_budget_exceeded")
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
        self.assertEqual([r.meta["seal_discovery_method"] for r in requests], ["seed", "homepage", "sitemap_probe"])
        self.assertTrue(all(isinstance(r, Request) and not r.dont_filter for r in requests))
        requests = asyncio.run(collect(MODULE.SeededSpider({}, self.context(mode="recheck"))))
        self.assertEqual(len(requests), 1)

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
        self.assertLess(settings_for(context)["DOWNLOADER_MIDDLEWARES"]["seal.robots.ArchivedRobotsMiddleware"], 30)


    def test_robots_cache_separates_http_and_https_permission(self):
        middleware = ArchivedRobotsMiddleware.__new__(ArchivedRobotsMiddleware)
        http_policy, https_policy = object(), object()
        middleware._parsers = {"http://example.org": http_policy, "https://example.org": https_policy}
        self.assertIs(asyncio.run(middleware.robot_parser(Request("http://example.org/a"))), http_policy)
        self.assertIs(asyncio.run(middleware.robot_parser(Request("https://example.org/a"))), https_policy)

    def test_policy_content_challenges_fail_closed_and_plain_policies_remain_valid(self):
        for body in (b"<!doctype html><form>Sign in</form>", b'<html>Challenge</html>', b'{"login":true}'):
            self.assertTrue(is_policy_document(body))
        for body in (b"", b"User-agent: *\nDisallow: /private", b"# An HTML comment\nUser-agent: *"):
            self.assertFalse(is_policy_document(body))


if __name__ == "__main__":
    unittest.main()
