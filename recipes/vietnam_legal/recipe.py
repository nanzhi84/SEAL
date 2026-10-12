"""Official legal pages, category forms and attachments; no business metadata projection."""

import scrapy

from seal.helpers import diagnostic, follow


class VietnamLegalSpider(scrapy.Spider):
    name = "vietnam_legal_documents"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.expand = context["mode"] != "recheck" or "POST" in context["config"]["methods"]

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(
                seed["url"],
                callback=self.parse_form
                if self.expand and seed["url"] == self.params.get("search", {}).get("url")
                else self.parse,
                errback=self.failed,
                meta={"seal_role": seed["role"]},
            )
        if not self.expand:
            return
        for url in self.params.get("membership_urls", []):
            yield scrapy.Request(
                url, callback=self.parse, errback=self.failed, meta={"seal_role": "list"}
            )
        if "search" in self.params and self.params["search"]["url"] not in {
            seed["url"] for seed in self.context["seeds"]
        }:
            yield scrapy.Request(
                self.params["search"]["url"],
                callback=self.parse_form,
                errback=self.failed,
                meta={"seal_role": "list"},
            )

    def failed(self, failure):
        response = getattr(failure.value, "response", None)
        if response is not None:
            yield diagnostic(response, "http_5xx" if response.status >= 500 else "http_error")

    def parse_form(self, response):
        search = self.params["search"]
        for category in search["category_ids"]:
            yield scrapy.FormRequest.from_response(
                response,
                formxpath="//form[@id='form1']",
                formdata={
                    "ctrl_191017_163$drdDocCategory": category,
                    "ctrl_191017_163$drdDocYear": str(search["year"]),
                },
                clickdata={"name": "ctrl_191017_163$btnSearch"},
                callback=self.parse,
                errback=self.failed,
                meta={
                    "seal_role": "list",
                    "seal_parent_url": response.url,
                    "seal_parent_snapshot_id": response.meta["seal_snapshot_id"],
                    "seal_parent_observation_id": response.meta["seal_observation_id"],
                },
            )

    def parse(self, response):
        if not self.expand or not hasattr(response, "css"):
            return
        for link in response.css('a[href*="docid="],a[href$=".pdf"],a[href$=".docx"]'):
            href = link.attrib["href"]
            yield follow(
                response,
                href,
                "attachment" if href.lower().endswith((".pdf", ".docx")) else "detail",
                self.parse,
                self.failed,
            )
