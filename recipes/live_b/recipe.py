"""Bounded public static resources from live-inspected B Source samples.

The education sample retains the original XLS and emits each complete workbook
as a URL-identified document with ordered sheet/cell evidence. Shenzhen's two
sampled notices still require OCR; no announcement metadata stands in for PDF text.
"""

import scrapy

from seal.core import SealError
from seal.helpers import (
    attachment_record,
    diagnostic,
    follow,
    html_record,
    input_reference,
    is_attachment_response,
    node_text,
    record_item,
    static_resources,
    xpath_locator,
)


class LiveBSpider(scrapy.Spider):
    name = "v12_live_b"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]

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
        if is_attachment_response(response):
            yield from self.parse_attachment(response, parent)
            return
        if self.params["source"] == "dd-468":
            yield html_record(
                response,
                title="h1",
                body=".TRS_Editor",
                date=None,
                record_type="education_notice",
            )
            selector = '.TRS_Editor a[href$=".xls"]'
        elif self.params["source"] == "dd-004":
            # The bounded business scope is the two explicitly named public
            # Noahxin notices. Other homepage channels are outside this sample.
            nodes = [
                node
                for node in response.css('a[href$=".pdf"]')
                if node.attrib["href"] in self.params["attachment_paths"]
            ]
            if len(nodes) != len(self.params["attachment_paths"]):
                yield diagnostic(response, "list_template_changed")
                return
            for node in nodes:
                yield self.announcement_record(response, node)
                yield follow(
                    response,
                    node.attrib["href"],
                    "attachment",
                    self.parse_attachment,
                    self.failed,
                    cb_kwargs={"parent": input_reference(response)},
                )
            return
        elif self.params["source"] == "dd-151":
            # GOV.UK also links the same file via an aria-hidden thumbnail.
            # The visible business attachment link is the inspected input.
            selector = 'a[href="' + self.params["attachment_paths"][0] + '"]:not([aria-hidden])'
        else:
            selector = 'a[href="' + self.params["attachment_paths"][0] + '"]'
        if len(response.css(selector)) != len(self.params["attachment_paths"]):
            yield diagnostic(response, "list_template_changed")
            return
        yield from static_resources(
            response,
            iframe_callback=self.parse,
            attachment_callback=self.parse_attachment,
            errback=self.failed,
            iframe_selector=None,
            attachment_selector=selector,
        )

    def announcement_record(self, response, node):
        dates = node.css("span.item_time")
        titles = [text.strip() for text in node.xpath("./text()").getall() if text.strip()]
        if len(dates) != 1 or len(titles) != 1:
            raise SealError("ambiguous_or_missing_field")
        title, path = titles[0], node.attrib["href"]
        data = {"title": title, "date": node_text(dates[0]), "attachment_path": path}
        locators = {"date": xpath_locator(dates[0])}
        # Attribute/text-node XPath extraction is not part of Runtime's element
        # locator contract. Exact raw text spans preserve these untransformed
        # source values and remain reproducible from the archived gb18030 body.
        for field, value in (("title", title), ("attachment_path", path)):
            if response.text.count(value) != 1:
                raise SealError("ambiguous_or_missing_field")
            start = response.text.index(value)
            locators[field] = {"kind": "text", "start": start, "end": start + len(value)}
        return record_item(
            response,
            record_type="public_announcement",
            record_key=path,
            data=data,
            locators=locators,
            key_locator=locators["attachment_path"].copy(),
        )

    def parse_attachment(self, response, parent=None):
        try:
            item = attachment_record(
                response, parent=parent, record_type="public_business_attachment"
            )
            headings = {
                "dd-050": ["中国海事仲裁委员会仲裁规则"],
                "dd-151": ["Corporate Plan", "2026 to 2027"],
            }
            if self.params["source"] in headings:
                self.pdf_title(item, headings[self.params["source"]])
            yield item
        except SealError as error:
            yield diagnostic(response, error.code)

    def pdf_title(self, item, parts):
        # Some files begin with page numbers or a publishing disclaimer. Their
        # own business headings remain verifiable on one actual PDF page.
        cursor = 0
        for segment in item["locators"]["body"]["segments"]:
            length = segment["end"] - segment["start"]
            text = item["data"]["body"][cursor : cursor + length]
            starts = [text.find(part) for part in parts]
            if all(start >= 0 for start in starts):
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
