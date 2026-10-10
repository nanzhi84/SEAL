"""Seed expansion and recursive public collection using Scrapy's sole scheduler.

Source-specific API/cursor contracts may subclass this ordinary Python recipe.
There is no guessed query enumeration or assumed website population.
"""

import scrapy
from scrapy.http import TextResponse

from seal.archive import request_key
from seal.core import SealError
from seal.discovery import mark_failed
from seal.helpers import (
    attachment_record,
    diagnostic,
    html_strategy_record,
    input_reference,
    is_attachment_response,
)
from seal.seeded import html_links, is_sitemap_response, origin_url, sitemap_links


class SeededSpider(scrapy.Spider):
    name = "seeded_public_site"
    seeded_collection = True
    discovery_defaults = {"max_depth": 12, "max_query_variants": 40, "exclude_patterns": []}

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        if not context["config"].get("robots"):
            raise SealError("seeded_robots_required")
        if context["config"].get("output_schema") != "record.v1":
            raise SealError("seeded_record_schema_required")
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]

    def request(self, url, role, method, *, parent=None, depth=0, optional=False):
        meta = {"seal_role": role, "seal_discovery_method": method, "seal_depth": depth}
        if optional:
            meta["_seal_optional_entry"] = True
            meta["handle_httpstatus_list"] = [404, 410]
        if parent is not None:
            meta.update(
                {
                    "seal_parent_url": parent.url,
                    "seal_parent_snapshot_id": parent.meta["seal_snapshot_id"],
                    "seal_parent_observation_id": parent.meta["seal_observation_id"],
                }
            )
            meta["seal_depth"] = parent.meta.get("seal_depth", 0) + 1
        return scrapy.Request(
            url,
            callback=self.parse,
            errback=self.failed,
            meta=meta,
            dont_filter=False,
            cb_kwargs={"parent": input_reference(parent)}
            if parent is not None and role == "attachment"
            else {},
        )

    async def start(self):
        for seed in self.context["seeds"]:
            yield self.request(seed["url"], seed["role"], "seed")
            if self.context["mode"] == "recheck":
                continue
            if self.params.get("expand_homepage", True):
                homepage = self.request(
                    origin_url(seed["url"], "/"), "list", "homepage", optional=True
                )
                homepage.meta["seal_parent_url"] = seed["url"]
                if self.restore_expansion(homepage):
                    yield homepage
            if self.params.get("discover_sitemaps", True):
                sitemap = self.request(
                    origin_url(seed["url"], "/sitemap.xml"),
                    "sitemap",
                    "sitemap_probe",
                    optional=True,
                )
                sitemap.meta["seal_parent_url"] = seed["url"]
                if self.restore_expansion(sitemap):
                    yield sitemap

    def restore_expansion(self, request):
        return self.context["mode"] != "replay" or any(
            entry["key"] == request_key(request) for entry in self.context["replay_inputs"]
        )

    def failed(self, failure):
        # Guard/robots rejections already carry authoritative ledger explanations.
        if getattr(failure.value, "response", None) is not None:
            response = failure.value.response
            yield diagnostic(response, "http_5xx" if response.status >= 500 else "http_error")

    def parse(self, response, parent=None):
        if response.status in (404, 410) and response.meta.get("_seal_optional_entry"):
            mark_failed(self.context, response.request, "optional_entry_missing")
            return
        if is_sitemap_response(response):
            try:
                for url, role in sitemap_links(response):
                    yield self.request(response.urljoin(url), role, "sitemap", parent=response)
            except SealError as exc:
                yield diagnostic(response, exc.code)
            return
        if is_attachment_response(response):
            try:
                yield attachment_record(response, parent=parent, record_type="document_attachment")
            except (SealError, ValueError, KeyError, TypeError) as exc:
                yield diagnostic(
                    response, exc.code if isinstance(exc, SealError) else "attachment_parse_failed"
                )
            return
        if not isinstance(response, TextResponse):
            yield diagnostic(response, "unsupported_content_type")
            return
        content_type = response.headers.get("Content-Type", b"").lower()
        if b"html" not in content_type and not response.body.lstrip().startswith((b"<!", b"<html")):
            # JSON paging is a source-specific Python contract; never claim a
            # generic single response has enumerated an unknown API population.
            yield diagnostic(
                response,
                "pagination_contract_missing"
                if b"json" in content_type
                else "unsupported_content_type",
            )
            return
        try:
            record = html_strategy_record(
                response, specific_rules=self.params.get("specific_rules", [])
            )
            self.crawler.stats.inc_value(
                "seal/" + response.meta.get("seal_html_strategy", "fallback") + "_documents"
            )
            yield record
        except SealError as exc:
            if exc.code == "fallback_non_document":
                self.crawler.stats.inc_value("seal/navigation_pages")
            else:
                yield diagnostic(response, exc.code)
                if exc.code == "access_control_detected":
                    return
        for url, role, method in html_links(response):
            yield self.request(url, role, method, parent=response)
        if self.params.get("discover_sitemaps", True):
            # robots policy was already archived by the native middleware. Emit
            # its declared sitemaps as regular candidates with policy parentage.
            policy = response.request.meta.get("_seal_robots_sitemaps", {})
            for url in policy.get("urls", []):
                candidate = self.request(url, "sitemap", "robots_sitemap", parent=response)
                candidate.meta.update(
                    {
                        "seal_parent_url": policy["url"],
                        "seal_parent_snapshot_id": policy["snapshot_id"],
                        "seal_parent_observation_id": policy["observation_id"],
                    }
                )
                # This parent is policy, not the HTML page currently parsed.
                candidate.meta["_seal_explicit_policy_parent"] = True
                yield candidate
