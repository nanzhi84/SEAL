"""Bounded ordinary Python adapters from inspected public source HTML.

Research bytes prove the selectors and discovery rules only. Live detail,
iframe and attachment acceptance remains blocked until responses are archived.
"""

import re

import scrapy

from seal.helpers import attachment_record, diagnostic, follow, html_record, static_resources


class SampleSpider(scrapy.Spider):
    name = "v12_reviewed_source_samples"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.source = params["source"]
        self.pages = set()
        self.details = set()

    async def start(self):
        for seed in self.context["seeds"]:
            role = seed["role"]
            callback = self.parse_attachment if role == "attachment" else self.parse
            yield scrapy.Request(
                seed["url"],
                callback=callback,
                errback=self.failed,
                meta={"seal_role": role},
            )

    def failed(self, failure):
        yield {"type": "diagnostic", "code": "download_failed"}

    def parse(self, response):
        content_type = response.headers.get("Content-Type", b"").decode("latin1").lower()
        # Detail URL recheck uses the historical detail role even for a PDF.
        # Route by the actual resource instead of assuming every detail is HTML.
        if "application/pdf" in content_type or response.url.lower().endswith((".pdf", ".xls")):
            yield from self.parse_attachment(response)
            return
        if self.source == "dd-116":
            if response.meta.get("seal_role") == "detail":
                yield html_record(
                    response,
                    title="h1",
                    body="#zoom",
                    date="time.no-date",
                    record_type="public_notice",
                )
            else:
                yield from self.parse_haikou_list(response)
        elif self.source == "dd-468":
            yield html_record(
                response,
                title="h1",
                body=".TRS_Editor",
                date="time.no-date",
                record_type="education_notice",
            )
            yield from self.attachments(response, '.TRS_Editor a[href$=".xls"]')
        elif self.source == "dd-004":
            # The entry mixes JS lists and HTML announcements. Only the
            # inspected static PDF announcement channel belongs to this sample.
            yield from self.attachments(
                response, 'a[href^="document/"][href$=".pdf"], a[href*="/document/"][href$=".pdf"]'
            )
        elif self.source == "dd-102":
            if response.meta.get("seal_role") == "iframe":
                # No retained iframe business bytes are available in this
                # checkout, so a business table adapter cannot be validated.
                yield diagnostic(response, "unverified_iframe_business_template")
                return
            frames = response.css('iframe[src*="/www/illegal?siteid=beijing"]')
            if len(frames) != 1:
                yield diagnostic(response, "ambiguous_or_missing_field")
                return
            yield from static_resources(
                response,
                iframe_callback=self.parse,
                attachment_callback=self.parse_attachment,
                errback=self.failed,
                iframe_selector='iframe[src*="/www/illegal?siteid=beijing"]',
                attachment_selector=None,
            )

    def attachments(self, response, selector):
        links = response.css(selector)
        if not links:
            yield diagnostic(response, "list_template_changed")
            return
        for request in static_resources(
            response,
            iframe_callback=self.parse,
            attachment_callback=self.parse_attachment,
            errback=self.failed,
            iframe_selector=None,
            attachment_selector=selector,
        ):
            url = request.url
            if url not in self.details:
                if len(self.details) >= self.params.get("max_details", 2):
                    request.meta["seal_helper_rejection"] = "sample_detail_limit"
                    yield request
                    continue
                self.details.add(url)
            yield request

    def parse_attachment(self, response, parent=None):
        # The shared extractor fails closed for XLS/scanned PDF; the response
        # has already been archived by Runtime before this callback runs.
        yield attachment_record(
            response,
            parent=parent,
            record_type="public_business_attachment",
        )

    def parse_haikou_list(self, response):
        if response.url in self.pages:
            yield diagnostic(response, "pagination_loop")
            return
        self.pages.add(response.url)
        links = response.css(".list-right_title a")
        if not links:
            yield diagnostic(response, "list_template_changed")
            return
        for link in links:
            href = link.attrib.get("href")
            if not href:
                yield diagnostic(response, "missing_document_href")
                continue
            url = response.urljoin(href)
            if url not in self.details:
                if len(self.details) >= self.params.get("max_details", 2):
                    yield follow(
                        response,
                        url,
                        "detail",
                        self.parse,
                        self.failed,
                        rejection="sample_detail_limit",
                    )
                    continue
                self.details.add(url)
            yield follow(response, url, "detail", self.parse, self.failed)
        # Read the site's declared static page naming rule; never execute JS.
        declarations = re.findall(
            r'createPageHTML\(\s*(\d+)\s*,\s*(\d+)\s*,\s*"index"\s*,\s*"shtml"\s*\)',
            response.text,
        )
        if len(declarations) != 1:
            yield diagnostic(response, "ambiguous_pagination")
            return
        count, current = map(int, declarations[0])
        if current + 1 >= count:
            return
        if len(self.pages) >= self.params.get("max_pages", 2):
            yield follow(
                response,
                f"index_{current + 1}.shtml",
                "list",
                self.parse,
                self.failed,
                rejection="pagination_limit",
            )
            return
        yield follow(
            response,
            f"index_{current + 1}.shtml",
            "list",
            self.parse,
            self.failed,
        )
