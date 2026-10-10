"""Frozen public publication scopes, with archived business-field locators.

Failure boundaries precede this adapter: changed/ambiguous templates, missing
business text, an unexpected representation, a damaged/textless PDF, or a
failed public GET must produce partial diagnostics, never navigation records.
Each Source is one inspected publication. No forms or embedded scripts run.
"""

import re
from io import BytesIO

import scrapy
from pypdf import PdfReader

from seal.core import SealError, public_url
from seal.helpers import diagnostic, node_text, record_item, xpath_locator


def matched(text, pattern, kind, **extra):
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1 or matches[0].lastindex != 1:
        raise SealError("ambiguous_or_missing_field")
    match = matches[0]
    value = match.group(1).strip()
    if not value:
        raise SealError("empty_document")
    return value, {"kind": kind, "start": match.start(1), "end": match.end(1), **extra}


def selected(response, params, field):
    css, xpath = params.get(field + "_css"), params.get(field + "_xpath")
    if bool(css) == bool(xpath):
        raise SealError("ambiguous_or_missing_field")
    nodes = response.css(css) if css else response.xpath(xpath)
    if len(nodes) != 1:
        raise SealError("ambiguous_or_missing_field")
    return nodes[0]


def business_segments(node):
    # Some inspected TRS editors embed CSS inside the body container. Keep
    # original bytes intact, and point only to actual text-bearing subtrees.
    # This excludes style/script rather than calling navigation the body.
    if str(node.root.tag).lower() in ("style", "script"):
        return []
    if not node.xpath(".//style | .//script"):
        return [node] if node_text(node) else []
    return [part for child in node.xpath("./*") for part in business_segments(child)]


class ExpandedASpider(scrapy.Spider):
    name = "v12_expanded_public_publications"

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

    def parse(self, response):
        try:
            data, locators = (
                self.pdf(response) if self.params["mode"] == "pdf" else self.html(response)
            )
            if len(data["body"]) < self.params["min_body_chars"] or not data["title"]:
                raise SealError("empty_document")
            url = public_url(response.url)
            yield record_item(
                response,
                record_type=self.params["record_type"],
                record_key=url,
                data=data,
                locators=locators,
                key_locator={"kind": "response_url"},
                detail_url=url,
            )
        except SealError as exc:
            yield diagnostic(response, exc.code)
        except (ValueError, TypeError, KeyError, AttributeError):
            yield diagnostic(response, "publication_parse_failed")

    def html(self, response):
        if not isinstance(response, scrapy.http.TextResponse):
            raise SealError("unexpected_content_type")
        title = selected(response, self.params, "title")
        parts = business_segments(selected(response, self.params, "body"))
        if not parts:
            raise SealError("empty_document")
        data = {"title": node_text(title), "body": "\n".join(node_text(part) for part in parts)}
        locators = {
            "title": xpath_locator(title),
            "body": {"kind": "segments", "segments": [xpath_locator(part) for part in parts]},
        }
        if self.params.get("date_pattern"):
            data["date"], locators["date"] = matched(
                response.text, self.params["date_pattern"], "text"
            )
        elif self.params.get("date_css") or self.params.get("date_xpath"):
            date = selected(response, self.params, "date")
            data["date"], locators["date"] = node_text(date), xpath_locator(date)
        return data, locators

    def pdf(self, response):
        # KIPO serves its inspected, publicly linked PDF as application/download
        # from a .do URL. Verified PDF magic is the representation boundary;
        # response headers and archived bytes are never changed to force parsing.
        if not response.body.startswith(b"%PDF-"):
            raise SealError("unexpected_content_type")
        try:
            reader = PdfReader(BytesIO(response.body), strict=True)
            if not 1 <= len(reader.pages) <= 50:
                raise SealError("pdf_page_scope_exceeded")
            texts = [(page.extract_text() or "").strip() for page in reader.pages]
        except SealError:
            raise
        except Exception:
            raise SealError("pdf_parse_failed") from None
        nonempty = [(number, text) for number, text in enumerate(texts, 1) if text]
        if not nonempty:
            raise SealError("pdf_text_layer_required")
        number, first = nonempty[0]
        title, title_locator = matched(first, self.params["title_pattern"], "pdf", page=number)
        data = {"title": title, "body": "\n".join(text for _, text in nonempty)}
        locators = {
            "title": title_locator,
            "body": {
                "kind": "segments",
                "segments": [
                    {"kind": "pdf", "page": n, "start": 0, "end": len(text)} for n, text in nonempty
                ],
            },
        }
        if self.params.get("date_pattern"):
            data["date"], locators["date"] = matched(
                first, self.params["date_pattern"], "pdf", page=number
            )
        return data, locators
