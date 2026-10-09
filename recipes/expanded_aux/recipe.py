"""Retain real notice text and unsupported industry-classification DOCX evidence.

The notice is useful metadata, never a substitute for the attached classification.
Both original attachments are archived and explicit partial diagnostics preserve
that business gap. No binary text extraction or fabricated locator is attempted.
"""

import scrapy

from seal.core import SealError
from seal.helpers import (
    attachment_record,
    diagnostic,
    follow,
    input_reference,
    node_text,
    record_item,
    xpath_locator,
)


class ClassificationNotice(scrapy.Spider):
    name = "classification_notice_and_original_documents"

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
        titles = response.css("h2")
        paragraphs = [node for node in response.css(".TRS_Editor p") if node_text(node)]
        if len(titles) != 1 or not paragraphs:
            yield diagnostic(response, "source_template_changed")
            return
        url = response.url
        yield record_item(
            response,
            record_type="industry_classification_notice",
            record_key=url,
            detail_url=url,
            data={
                "title": node_text(titles[0]),
                "body": "\n".join(node_text(node) for node in paragraphs),
            },
            locators={
                "title": xpath_locator(titles[0]),
                "body": {
                    "kind": "segments",
                    "segments": [xpath_locator(node) for node in paragraphs],
                },
            },
            key_locator={"kind": "response_url"},
        )
        links = {response.urljoin(node.attrib["href"]) for node in response.css("a[href]")}
        if not set(self.params["attachment_urls"]) <= links:
            yield diagnostic(response, "missing_document_href")
            return
        for target in self.params["attachment_urls"]:
            yield follow(
                response,
                target,
                "attachment",
                self.attachment,
                self.failed,
                cb_kwargs={"parent": input_reference(response)},
            )

    def attachment(self, response, parent=None):
        try:
            yield attachment_record(response, parent=parent)
        except SealError as error:
            yield diagnostic(response, error.code)
