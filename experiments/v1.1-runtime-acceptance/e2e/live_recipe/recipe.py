"""Bound discovery only; detail parsing is the repository's existing GenericSpider."""

import scrapy
from generic import GenericSpider


class LiveSpider(GenericSpider):
    name = "public_source_smoke"

    def parse_list(self, response):
        if response.url in self.list_urls:
            yield {"type": "diagnostic", "code": "pagination_loop"}
            return
        self.list_urls.add(response.url)
        links = response.css(self.params["links"])[: self.params["per_page"]]
        if not links:
            yield {"type": "diagnostic", "code": "list_template_changed"}
            return
        for link in links:
            url = response.urljoin(link.attrib["href"])
            yield {
                "type": "document_ref",
                "url": url,
                "snapshot_id": response.meta["seal_snapshot_id"],
            }
            yield scrapy.Request(
                url, callback=self.parse_detail, errback=self.failed, meta={"seal_role": "detail"}
            )
        if len(self.list_urls) < self.params["pages"]:
            href = response.css(self.params["next_page"]).attrib.get("href")
            if href:
                yield scrapy.Request(
                    response.urljoin(href),
                    callback=self.parse_list,
                    errback=self.failed,
                    meta={"seal_role": "list"},
                )
