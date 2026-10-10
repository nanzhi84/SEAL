"""Bounded static public business documents from inspected B samples.

Each Binding declares one real document and its reviewed field selectors.
URL identity is the publication identity, never a table row position. The
Ningxia notification requires its actual text PDF; the HTML attachment title
is not emitted as a business document. Unsupported annexes are outside that
explicit notice-only scope. JS is never executed: the film permit table's
published _bima, _badw and _biju literal values are located in archived raw text.
"""

import re

import scrapy

from seal.core import SealError, public_url
from seal.helpers import (
    attachment_record,
    diagnostic,
    follow,
    input_reference,
    is_attachment_response,
    node_text,
    record_item,
    selected_field,
    xpath_locator,
)


class ExpandedB2Spider(scrapy.Spider):
    name = "expanded_static_public_documents_b2"

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
        try:
            if is_attachment_response(response):
                yield self.pdf_record(response, parent)
            elif public_url(response.url) not in self.params["urls"]:
                raise SealError("unexpected_url_scope")
            elif self.params["mode"] == "pdf":
                # This exact visible link was inspected before implementation.
                # Do not fabricate a URL or treat the attachment title as body.
                for path in self.params["attachment_paths"]:
                    nodes = response.css('a[href="' + path + '"]')
                    if len(nodes) != 1:
                        raise SealError("list_template_changed")
                    yield follow(
                        response,
                        nodes[0].attrib["href"],
                        "attachment",
                        self.parse,
                        self.failed,
                        cb_kwargs={"parent": input_reference(response)},
                    )
            else:
                yield self.html_record(response)
        except (KeyError, TypeError, ValueError, SealError) as exc:
            yield diagnostic(
                response, exc.code if isinstance(exc, SealError) else "source_schema_changed"
            )

    def html_record(self, response):
        title, title_locator = selected_field(response, self.params["title_selector"])
        nodes = response.css(self.params["body_selector"])
        if not nodes or (not self.params["body_segments"] and len(nodes) != 1):
            raise SealError("ambiguous_or_missing_field")
        if "table_rows" in self.params:
            counts = [len(table.css("tbody > tr")) - 1 for table in response.css(".hmc4Table")]
            if counts != self.params["table_rows"]:
                raise SealError("list_template_changed")
        script_spans = self.script_spans(response) if "table_rows" in self.params else {}
        values, locators = [], []
        for node in nodes:
            if node.css("script,style"):
                value, locator = self.public_literal(response, node, script_spans)
            else:
                value, locator = node_text(node), xpath_locator(node)
            # Empty formatting paragraphs/cells have no business text. A
            # delimiter is never allowed to turn them into unlocated content.
            if not value and self.params["body_segments"]:
                continue
            values.append(value)
            locators.append(locator)
        body = "\n".join(values)
        if not title or not body.strip():
            raise SealError("empty_document")
        body_locator = (
            {"kind": "segments", "separator": "\n", "segments": locators}
            if self.params["body_segments"]
            else locators[0]
        )
        url = public_url(response.url)
        return record_item(
            response,
            record_type="public_business_publication",
            record_key=url,
            data={"title": title, "body": body},
            locators={"title": title_locator, "body": body_locator},
            key_locator={"kind": "response_url"},
            detail_url=url,
        )

    def script_spans(self, response):
        raw = list(re.finditer(r"<script\b[^>]*>([\s\S]*?)</script\s*>", response.text, re.I))
        nodes = response.css("script")
        if len(raw) != len(nodes) or any(
            match[1] != node.xpath("./text()").get(default="")
            for match, node in zip(raw, nodes, strict=True)
        ):
            raise SealError("source_schema_changed")
        # Mapping the reviewed raw script sequence to DOM paths disambiguates
        # repeated business names without assuming a string occurs only once.
        return {
            xpath_locator(node)["path"]: (match[1], match.start(1))
            for match, node in zip(raw, nodes, strict=True)
        }

    def public_literal(self, response, node, spans):
        scripts = node.css("script")
        if len(scripts) != 1 or node.css("style"):
            raise SealError("unexpected_script_in_business_body")
        text, base = spans[xpath_locator(scripts[0])["path"]]
        if self.params["source"] == "dd-448":
            match = re.search(r"var (_badw|_biju) = '([^'\\]*)';", text)
            confirmed = match is not None and "document.write(" + match[1] + ")" in text
            group = 2
        elif self.params["source"] == "dd-449":
            match = re.search(r'var _bima="([a-zA-Z0-9]+)";', text)
            confirmed = match is not None and "document.write(_bima.replaceAll" in text
            group = 1
        else:
            raise SealError("unexpected_script_in_business_body")
        if not confirmed:
            raise SealError("source_schema_changed")
        start, end = base + match.start(group), base + match.end(group)
        return match[group], {"kind": "text", "start": start, "end": end}

    def pdf_record(self, response, parent):
        if self.params["mode"] != "pdf":
            raise SealError("unexpected_attachment")
        item = attachment_record(response, parent=parent, record_type="public_text_pdf_notice")
        # The first PDF line is a page-number artifact. Preserve the real
        # source heading as adjacent PDF text spans, not the HTML metadata.
        cursor = 0
        parts = self.params["pdf_title_parts"]
        for segment in item["locators"]["body"]["segments"]:
            length = segment["end"] - segment["start"]
            text = item["data"]["body"][cursor : cursor + length]
            starts = [text.find(part) for part in parts]
            if all(start >= 0 for start in starts):
                item["data"]["title"] = "".join(parts)
                item["locators"]["title"] = {
                    "kind": "segments",
                    "separator": "",
                    "segments": [
                        {**segment, "start": start, "end": start + len(part)}
                        for part, start in zip(parts, starts, strict=True)
                    ],
                }
                return item
            cursor += length + 1
        raise SealError("ambiguous_or_missing_field")
