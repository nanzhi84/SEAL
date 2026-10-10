"""Seed expansion and recursive public collection using Scrapy's sole scheduler.

Source-specific API/cursor contracts may subclass this ordinary Python recipe.
There is no guessed query enumeration or assumed website population.
"""

import scrapy
from scrapy.http import TextResponse

from seal.core import SealError
from seal.discovery import mark_failed
from seal.helpers import (
    attachment_record,
    diagnostic,
    html_strategy_record,
    input_reference,
    is_attachment_response,
)
from seal.scope import attachment_response_scope
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
        # A Recheck (and its Replay) observes only its frozen targets. Auxiliary
        # robots policy fetches remain a Runtime guard, not recipe exploration.
        self.expansion_enabled = (
            context["mode"] != "recheck" and context.get("recheck_plan") is None
        )
        self._discovered_policy_sitemaps = set()

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
            # The URL can hide an attachment suffix; final Content-Type decides
            # whether parse consumes this parent as a supplementary input.
            cb_kwargs={"parent": input_reference(parent)} if parent is not None else {},
        )

    async def start(self):
        for seed in self.context["seeds"]:
            yield self.request(seed["url"], seed["role"], "seed")
            if not self.expansion_enabled:
                continue
            if self.params.get("expand_homepage", True):
                homepage = self.request(
                    origin_url(seed["url"], "/"), "list", "homepage", optional=True
                )
                homepage.meta["seal_parent_url"] = seed["url"]
                yield homepage
            if self.params.get("discover_sitemaps", True):
                sitemap = self.request(
                    origin_url(seed["url"], "/sitemap.xml"),
                    "sitemap",
                    "sitemap_probe",
                    optional=True,
                )
                sitemap.meta["seal_parent_url"] = seed["url"]
                yield sitemap

    def failed(self, failure):
        # Guard/robots rejections already carry authoritative ledger explanations.
        if getattr(failure.value, "response", None) is not None:
            response = failure.value.response
            yield diagnostic(response, "http_5xx" if response.status >= 500 else "http_error")

    def parse(self, response, parent=None):
        if response.status in (404, 410) and response.meta.get("_seal_optional_entry"):
            mark_failed(self.context, response.request, "optional_entry_missing")
            return
        yield from self.policy_sitemaps(response)
        if is_sitemap_response(response):
            if not self.expansion_enabled:
                return
            try:
                for url, role in sitemap_links(
                    response, max_size=self.context["config"]["budget"]["response_bytes"]
                ):
                    yield self.request(response.urljoin(url), role, "sitemap", parent=response)
            except SealError as exc:
                yield diagnostic(response, exc.code)
            return
        if is_attachment_response(response):
            parent_url = response.meta.get("seal_parent_url", response.url)
            code = attachment_response_scope(self.context["config"], response.url, parent_url)
            if code:
                # A suffix-free URL can become an attachment only after download.
                # Preserve its archive without promoting an implicit cross-host
                # attachment outside the explicit Source resource contract.
                mark_failed(self.context, response.request, code)
                yield diagnostic(response, code)
                return
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
        # No implicit attachment/iframe contract exists for this generic
        # Recheck. Direct frozen attachment targets are parsed above as usual.
        if not self.expansion_enabled:
            return
        for url, role, method in html_links(response):
            yield self.request(url, role, method, parent=response)

    def policy_sitemaps(self, response):
        if self.expansion_enabled and self.params.get("discover_sitemaps", True):
            # robots policy was already archived by the native middleware. Emit
            # its declared sitemaps as regular candidates with policy parentage.
            policy = response.request.meta.get("_seal_robots_sitemaps", {})
            if not policy.get("urls"):
                return
            policy_version = (origin_url(policy["url"], "/"), policy["snapshot_id"])
            if policy_version in self._discovered_policy_sitemaps:
                return
            # Discover a policy version once per Crawl before Scheduler sees
            # candidates. Later pages keep their own ordinary link evidence.
            self._discovered_policy_sitemaps.add(policy_version)
            for url in dict.fromkeys(policy["urls"]):
                candidate = self.request(url, "sitemap", "robots_sitemap", parent=response)
                candidate.meta.update(
                    {
                        "seal_parent_url": policy["url"],
                        "seal_parent_snapshot_id": policy["snapshot_id"],
                        "seal_parent_observation_id": policy["observation_id"],
                    }
                )
                candidate.cb_kwargs["parent"] = {
                    "snapshot_id": policy["snapshot_id"],
                    "observation_id": policy["observation_id"],
                }
                # This parent is policy, not the HTML page currently parsed.
                candidate.meta["_seal_explicit_policy_parent"] = True
                yield candidate
