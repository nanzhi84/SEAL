"""Reviewed HTML pagination, iframe and attachment discovery; no content extraction."""

import scrapy
from scrapy.http import TextResponse

from seal.helpers import diagnostic, follow, is_attachment_response, link_requests, static_resources


class HeterogeneousSpider(scrapy.Spider):
    name = "heterogeneous_public_documents"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.pages = set()
        self.expand = context["mode"] != "recheck" or "POST" in context["config"]["methods"]

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(
                seed["url"],
                callback=self.parse,
                errback=self.failed,
                meta={"seal_role": seed["role"]},
            )

    def failed(self, failure):
        response = getattr(failure.value, "response", None)
        if response is not None:
            yield diagnostic(response, "http_5xx" if response.status >= 500 else "http_error")

    def parse_options(self, response):
        return self.params.get("options", {}) if response.meta.get("seal_role") == "detail" else {}

    def parse(self, response, parent=None):
        if (
            not self.expand
            or not isinstance(response, TextResponse)
            or is_attachment_response(response)
        ):
            return
        if b"html" not in response.headers.get("Content-Type", b""):
            return
        yield from static_resources(
            response,
            iframe_callback=self.parse,
            attachment_callback=self.parse,
            errback=self.failed,
            iframe_selector=self.params.get("iframes", "iframe[src]"),
            attachment_selector=self.params.get("attachments", "a.attachment[href]"),
        )
        if response.meta.get("seal_role") != "list":
            return
        self.pages.add(response.url)
        yield from link_requests(
            response,
            self.params.get("links", ".documents a[href]"),
            "detail",
            self.parse,
            self.failed,
        )
        pages = response.css(self.params.get("next_page", "a.next[href]"))
        if len(pages) > 1:
            yield diagnostic(response, "ambiguous_pagination")
        for page in pages:
            url = response.urljoin(page.attrib["href"])
            if url in self.pages:
                yield diagnostic(response, "pagination_loop")
            yield follow(response, url, "list", self.parse, self.failed)
