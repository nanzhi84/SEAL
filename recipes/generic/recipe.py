"""Minimal configurable HTML link discovery with format parsing owned by Runtime."""

import scrapy
from scrapy.http import TextResponse

from seal.helpers import diagnostic, follow, link_requests


class GenericSpider(scrapy.Spider):
    name = "generic_public_document"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.pages = set()

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

    def parse(self, response):
        if (
            self.context["mode"] == "recheck"
            or not isinstance(response, TextResponse)
            or response.meta.get("seal_role") != "list"
        ):
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
