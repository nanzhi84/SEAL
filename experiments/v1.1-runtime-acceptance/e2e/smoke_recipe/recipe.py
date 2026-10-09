"""Trusted fixture-only Recipe; never reads expected answers or fixture files."""

import re

import scrapy

TITLE_SELECTOR = "h1.title, h2[data-field='title']"


def xpath(node):
    return {"kind": "xpath", "path": node.root.getroottree().getpath(node.root)}


def text(node):
    return " ".join(node.xpath(".//text()").getall()).strip()


class FixtureSpider(scrapy.Spider):
    name = "engineering_smoke"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.context = context
        self.params = params

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(
                seed["url"],
                callback=self.parse,
                errback=self.failed,
                meta={"seal_role": seed["role"]},
                dont_filter=True,
            )

    def failed(self, failure):
        yield {"type": "diagnostic", "code": "download_failed"}

    def parse(self, response):
        if response.meta["seal_role"] == "list":
            for link in response.css("ul.items a"):
                url = response.urljoin(link.attrib["href"])
                yield {
                    "type": "document_ref",
                    "url": url,
                    "snapshot_id": response.meta["seal_snapshot_id"],
                }
                yield scrapy.Request(
                    url, callback=self.parse, errback=self.failed, meta={"seal_role": "detail"}
                )
            for href in response.css("a.next::attr(href)").getall():
                yield scrapy.Request(
                    response.urljoin(href),
                    callback=self.parse,
                    errback=self.failed,
                    meta={"seal_role": "list"},
                )
            return
        title = response.css(TITLE_SELECTOR)
        body = response.css(".article-body, [data-field='content']")
        if len(title) != 1 or len(body) != 1:
            yield {"type": "diagnostic", "code": "ambiguous_or_missing_field"}
            return
        parts = body.css("p, tr")
        values = []
        for part in parts:
            cells = part.css("th, td")
            values.append("：".join(text(cell) for cell in cells) if cells else text(part))
        item = {
            "type": "candidate",
            "url": response.url,
            "snapshot_id": response.meta["seal_snapshot_id"],
            "observation_id": response.meta["seal_observation_id"],
            "title": text(title[0]),
            "body": "\n".join(values),
            "date": None,
            "locators": {
                "title": xpath(title[0]),
                "body": {
                    "kind": "segments",
                    "segments": [
                        {
                            "kind": "segments",
                            "separator": "：",
                            "segments": [xpath(cell) for cell in p.css("th, td")],
                        }
                        if p.css("th, td")
                        else xpath(p)
                        for p in parts
                    ],
                },
            },
        }
        date = response.css(".published, [data-field='publication-date']")
        if date:
            raw = date[0].attrib.get("datetime") or text(date[0])
            match = re.search(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?", raw)
            if match:
                y, m, d = map(int, match.groups())
                item["date"] = f"{y:04d}-{m:02d}-{d:02d}"
                # Preserve the raw character range; Runtime recomputes the ISO date.
                start = response.text.index(match.group())
                item["locators"]["date"] = {
                    "kind": "text",
                    "start": start,
                    "end": start + len(match.group()),
                    "transform": "date_iso",
                }
        fault = self.params.get("fault")
        if fault and response.url.endswith(("/notice/alternate", "/probe/invalid-date")):
            locator = item["locators"]["date"]
            if fault == "unknown_transform":
                locator["transform"] = "arbitrary_python"
            elif fault == "wrong_locator":
                locator.update(start=0, end=15)
            elif fault == "wrong_output":
                item["date"] = "2026-08-31"
            elif fault == "invalid_date":
                locator.update(
                    start=response.text.index("2026年") + 5, end=response.text.index("2026年") + 8
                )
            elif fault == "empty_segments":
                item["locators"]["body"] = {"kind": "segments", "segments": []}
            elif fault == "calendar_date":
                item["date"] = "2026-02-28"
            elif fault == "wrong_segment":
                item["locators"]["body"]["segments"][0] = item["locators"]["title"]
            elif fault == "nested_input":
                item["locators"]["body"]["segments"][0]["snapshot_id"] = "0" * 64
            elif fault == "fabricated_separator":
                item["locators"]["body"]["separator"] = "fabricated body"
        yield item
