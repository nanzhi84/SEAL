"""Reviewed examples: HTML details, stable-key tables and nested JSON GET APIs."""

import scrapy

from seal.core import SealError
from seal.helpers import (
    attachment_record,
    follow,
    html_record,
    is_attachment_response,
    json_records,
    link_requests,
    static_resources,
    table_records,
)


class HeterogeneousSpider(scrapy.Spider):
    name = "heterogeneous_public_records"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]
        # Observed parser history diagnoses explicit pagination cycles. Native
        # Scrapy Scheduler/Dupefilter still decides which requests are fetched.
        self.parsed_lists = set()

    async def start(self):
        for seed in self.context["seeds"]:
            role = seed["role"]
            callback = (
                self.parse_attachment
                if role == "attachment"
                else self.parse_detail
                if role == "detail"
                else self.parse_iframe
                if role == "iframe"
                else self.parse_list
            )
            yield scrapy.Request(
                seed["url"], callback=callback, errback=self.failed, meta={"seal_role": role}
            )

    def diagnostic(self, response, code):
        return {
            "type": "diagnostic",
            "code": code,
            "snapshot_id": response.meta["seal_snapshot_id"],
            "observation_id": response.meta["seal_observation_id"],
        }

    def failed(self, failure):
        response = getattr(failure.value, "response", None)
        status = response.status if response is not None else None
        code = (
            "download_failed"
            if status is None
            else "source_rate_limited"
            if status == 429
            else "unexpected_304"
            if status == 304
            else "http_5xx"
            if status >= 500
            else "http_error"
        )
        yield {"type": "diagnostic", "code": code}

    def records(self, response, mode):
        common = {
            "record_type": self.params.get(
                "record_type", "document" if mode == "html" else "entity"
            ),
            "schema_version": self.params.get("schema_version", "record.v1"),
        }
        if mode == "json":
            yield from json_records(
                response,
                pointer=self.params.get("json_pointer", "/data/data/dataList"),
                key_field=self.params.get("key_field", "id"),
                detail_url_field=self.params.get("detail_url_field"),
                **common,
            )
        elif mode == "table":
            yield from table_records(
                response,
                rows=self.params["rows"],
                fields=self.params["table_fields"],
                key_field=self.params["key_field"],
                **common,
            )
        else:
            yield html_record(
                response,
                title=self.params.get("title", "h1"),
                body=self.params.get("body", "article"),
                date=self.params.get("date", "time"),
                **common,
            )

    def resources(self, response):
        yield from static_resources(
            response,
            iframe_callback=self.parse_iframe,
            attachment_callback=self.parse_attachment,
            errback=self.failed,
            iframe_selector=self.params.get("iframes", "iframe[src]"),
            attachment_selector=self.params.get("attachments", "a.attachment[href]"),
        )

    def parse_list(self, response):
        self.parsed_lists.add(response.url)
        mode = self.params.get("mode", "html")
        if mode == "html":
            links = list(
                link_requests(
                    response,
                    self.params.get("links", ".documents a[href]"),
                    "detail",
                    self.parse_detail,
                    self.failed,
                )
            )
            resources = list(self.resources(response))
            if not links and not resources:
                yield self.diagnostic(response, "list_template_changed")
            yield from links
            yield from resources
        else:
            try:
                yield from self.records(response, mode)
            except (KeyError, TypeError, ValueError, SealError) as exc:
                yield self.diagnostic(
                    response, exc.code if isinstance(exc, SealError) else "record_parse_failed"
                )
            if mode == "table":
                yield from self.resources(response)
            else:
                # API pagination parameters/envelope semantics stay in reviewed
                # source Python, rather than a generic query-building language.
                return
        pages = response.css(self.params.get("next_page", "a.next[href]"))
        if len(pages) > 1:
            yield self.diagnostic(response, "ambiguous_pagination")
        for page in pages:
            url = response.urljoin(page.attrib["href"])
            if url in self.parsed_lists:
                yield self.diagnostic(response, "pagination_loop")
            # The discovery is retained even when the native dupefilter drops it.
            yield follow(response, url, "list", self.parse_list, self.failed)

    def parse_detail(self, response):
        # Recheck preserves V1.1's detail-URL seed role. Dispatch on the actual
        # archived representation rather than assuming every detail is HTML.
        if is_attachment_response(response):
            yield from self.parse_attachment(response)
            return
        try:
            yield from self.records(response, "html")
        except (KeyError, TypeError, ValueError, SealError) as exc:
            yield self.diagnostic(
                response, exc.code if isinstance(exc, SealError) else "record_parse_failed"
            )
        yield from self.resources(response)

    def parse_iframe(self, response):
        try:
            yield from self.records(response, self.params.get("iframe_mode", "html"))
        except (KeyError, TypeError, ValueError, SealError) as exc:
            yield self.diagnostic(
                response, exc.code if isinstance(exc, SealError) else "record_parse_failed"
            )
        yield from self.resources(response)

    def parse_attachment(self, response, parent=None):
        try:
            yield attachment_record(
                response,
                parent=parent,
                record_type=self.params.get("attachment_record_type", "document_attachment"),
                schema_version=self.params.get("schema_version", "record.v1"),
            )
        except (KeyError, TypeError, ValueError, SealError) as exc:
            yield self.diagnostic(
                response, exc.code if isinstance(exc, SealError) else "attachment_parse_failed"
            )
