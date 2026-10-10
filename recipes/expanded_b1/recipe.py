"""Frozen public B1 HTML/PDF samples, following actual inspected declarations.

Business extraction stays at source level. Requests, scope enforcement, archives,
stable identity, rechecks and Replay remain Runtime responsibilities. The only
script interpretation is literal public pagination/attachment URLs, never JS
execution, cookies, credentials, forms or a browser.
"""

import re

import scrapy

from seal.core import SealError, public_url
from seal.helpers import (
    attachment_record,
    diagnostic,
    follow,
    html_record,
    input_reference,
    is_attachment_response,
)


class ExpandedB1Spider(scrapy.Spider):
    name = "expanded_b1_static_public_documents"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.documents = {item["url"]: item for item in params["documents"]}

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(
                seed["url"],
                callback=self.parse,
                errback=self.failed,
                meta={"seal_role": seed["role"]},
            )

    def failed(self, failure):
        yield {"type": "diagnostic", "code": "download_failed"}

    def parse(self, response, parent=None):
        url = public_url(response.url)
        if is_attachment_response(response):
            try:
                item = attachment_record(
                    response, parent=parent, record_type="public_business_attachment"
                )
                parts = self.params["pdf_titles"].get(url)
                if parts:
                    self.pdf_title(item, parts)
                yield item
            except SealError as error:
                yield diagnostic(response, error.code)
            return
        if url in self.documents:
            document = self.documents[url]
            try:
                yield html_record(
                    response,
                    title=document["title"],
                    body=document["body"],
                    date=None,
                    record_type="public_business_document",
                )
            except SealError as error:
                yield diagnostic(response, error.code)
        for route in self.params["routes"]:
            if route["parent"] != url:
                continue
            declared = self.declared_url(response, route)
            if declared is None:
                yield diagnostic(response, "list_template_changed")
                continue
            yield follow(
                response,
                declared,
                route["role"],
                self.parse,
                self.failed,
                cb_kwargs={"parent": input_reference(response)},
            )

    def declared_url(self, response, route):
        target = route["url"]
        if route["mechanism"] == "href":
            for node in response.css("a[href]"):
                href = node.attrib["href"].strip()
                if href.lower().startswith(("javascript:", "#", "mailto:")):
                    continue
                if public_url(response.urljoin(href)) == target:
                    return target
        elif route["mechanism"] == "script_attachment":
            for script in response.css(".article_detail script"):
                for path in re.findall(
                    r"window\.location\.href\s*=\s*[\"']([^\"']+)[\"']",
                    script.xpath("string(.)").get(),
                ):
                    if public_url(response.urljoin(path)) == target:
                        return target
        elif route["mechanism"] == "script_page":
            # The inspected public list's page(pn) literally constructs this
            # GET prefix. Sample page 2 is explicit, bounded and independently
            # verified; this is not an inferred API or arbitrary URL search.
            prefix = "disclosure.do?articleType=increase&pageNo="
            scripts = response.css("script").xpath("string(.)").getall()
            if any(prefix in text for text in scripts):
                declared = public_url(response.urljoin(prefix + "2"))
                if declared == target:
                    return declared
        return None

    def pdf_title(self, item, parts):
        cursor = 0
        for segment in item["locators"]["body"]["segments"]:
            length = segment["end"] - segment["start"]
            text = item["data"]["body"][cursor : cursor + length]
            starts = [text.find(part) for part in parts]
            if all(
                start >= 0 and text.count(part) == 1
                for part, start in zip(parts, starts, strict=True)
            ):
                item["data"]["title"] = " ".join(parts)
                item["locators"]["title"] = {
                    "kind": "segments",
                    "separator": " ",
                    "segments": [
                        {**segment, "start": start, "end": start + len(part)}
                        for part, start in zip(parts, starts, strict=True)
                    ],
                }
                return
            cursor += length + 1
        raise SealError("ambiguous_or_missing_field")
