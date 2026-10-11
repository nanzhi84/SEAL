"""Archive and parse public resources before optional HTML business extraction."""

import scrapy
from scrapy.http import TextResponse

from seal.core import SealError
from seal.discovery import mark_failed
from seal.helpers import (
    diagnostic,
    document_item,
    follow,
    html_record,
    is_attachment_response,
    save_document,
)
from seal.scope import attachment_response_scope
from seal.seeded import html_links, is_sitemap_response, origin_url, sitemap_links


class DocumentsSpider(scrapy.Spider):
    name = "public_document_pipeline"
    seeded_collection = True
    discovery_defaults = {"max_depth": 12, "max_query_variants": 40, "exclude_patterns": []}

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        if not context["config"].get("robots"):
            raise SealError("seeded_robots_required")
        if context["config"].get("output_schema") != "document.v1":
            raise SealError("document_schema_required")
        self.params, self.context = params, context
        self.options = params.get("options", {})
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.expansion_enabled = (
            context["mode"] != "recheck" and context.get("recheck_plan") is None
        )
        self._discovered_policy_sitemaps = set()

    def request(self, url, role, method, *, parent=None, optional=False):
        if parent is None:
            candidate = scrapy.Request(url, callback=self.parse, errback=self.failed)
            candidate.meta.update(seal_role=role, seal_depth=0)
        else:
            candidate = follow(parent, url, role, self.parse, self.failed)
            candidate.meta["seal_depth"] = parent.meta.get("seal_depth", 0) + 1
        candidate.meta["seal_discovery_method"] = method
        if optional:
            candidate.meta["_seal_optional_entry"] = True
            candidate.meta["handle_httpstatus_list"] = [404, 410]
        return candidate

    async def start(self):
        for seed in self.context["seeds"]:
            yield self.request(seed["url"], seed["role"], "seed")
            if not self.expansion_enabled:
                continue
            if self.params.get("expand_homepage", False):
                homepage = self.request(
                    origin_url(seed["url"], "/"), "list", "homepage", optional=True
                )
                homepage.meta["seal_parent_url"] = seed["url"]
                yield homepage
            if self.params.get("discover_sitemaps", False):
                sitemap = self.request(
                    origin_url(seed["url"], "/sitemap.xml"),
                    "sitemap",
                    "sitemap_probe",
                    optional=True,
                )
                sitemap.meta["seal_parent_url"] = seed["url"]
                yield sitemap

    def failed(self, failure):
        response = getattr(failure.value, "response", None)
        if response is not None:
            yield diagnostic(response, "http_5xx" if response.status >= 500 else "http_error")

    def parse(self, response):
        if response.status in (404, 410) and response.meta.get("_seal_optional_entry"):
            mark_failed(
                self.crawler.settings["SEAL_CONTEXT"], response.request, "optional_entry_missing"
            )
            return
        yield from self.policy_sitemaps(response)
        if is_sitemap_response(response):
            if self.expansion_enabled:
                try:
                    for url, role in sitemap_links(
                        response, max_size=self.context["config"]["budget"]["response_bytes"]
                    ):
                        yield self.request(response.urljoin(url), role, "sitemap", parent=response)
                except SealError as exc:
                    yield diagnostic(response, exc.code)
            return
        if is_attachment_response(response):
            code = attachment_response_scope(
                self.context["config"],
                response.url,
                response.meta.get("seal_parent_url", response.url),
            )
            if code:
                mark_failed(self.crawler.settings["SEAL_CONTEXT"], response.request, code)
                yield diagnostic(response, code)
                return
        html = isinstance(response, TextResponse) and (
            b"html" in response.headers.get("Content-Type", b"").lower()
            or response.body.lstrip().lower().startswith((b"<!doctype html", b"<html"))
        )
        try:
            if self.params.get("extract_business", False) and html:
                reference = save_document(response, self.context, self.options)
                # Navigation pages still become documents. Business extraction is
                # limited to pages with the explicitly selected detail fields.
                title = self.options.get("title_css", "h1")
                body = self.options.get("body_css", "article")
                if len(response.css(title)) == len(response.css(body)) == 1:
                    record = html_record(response, title=title, body=body, date=None)
                    record["data_schema_id"] = self.context["field_contract"]["data_schema"]["$id"]
                    record["document_versions"] = [reference]
                    yield record
            else:
                yield document_item(response, self.options)
        except (SealError, ValueError, KeyError, TypeError) as exc:
            yield diagnostic(
                response, exc.code if isinstance(exc, SealError) else "document_parse_failed"
            )
        # Link discovery does not depend on a successful content selection or
        # business projection. Scrapy and Runtime retain scope and deduplication.
        if html and self.expansion_enabled:
            for url, role, method in html_links(response):
                yield self.request(url, role, method, parent=response)

    def policy_sitemaps(self, response):
        if not self.expansion_enabled or not self.params.get("discover_sitemaps", False):
            return
        policy = response.request.meta.get("_seal_robots_sitemaps", {})
        if not policy.get("urls"):
            return
        version = (origin_url(policy["url"], "/"), policy["snapshot_id"])
        if version in self._discovered_policy_sitemaps:
            return
        self._discovered_policy_sitemaps.add(version)
        for url in dict.fromkeys(policy["urls"]):
            candidate = self.request(url, "sitemap", "robots_sitemap", parent=response)
            candidate.meta.update(
                seal_parent_url=policy["url"],
                seal_parent_snapshot_id=policy["snapshot_id"],
                seal_parent_observation_id=policy["observation_id"],
                _seal_explicit_policy_parent=True,
            )
            yield candidate
