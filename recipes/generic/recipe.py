"""Reviewed example recipe: paginated HTML and single-resource text/JSON/PDF."""

import json
from io import BytesIO

import scrapy
from pypdf import PdfReader


class GenericSpider(scrapy.Spider):
    name = "generic_public_document"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.list_urls = set()

    async def start(self):
        for seed in self.context["seeds"]:
            callback = self.parse_list if seed["role"] == "list" else self.parse_detail
            yield scrapy.Request(
                seed["url"],
                callback=callback,
                errback=self.failed,
                dont_filter=True,
                meta={"seal_role": seed["role"]},
            )

    def failed(self, failure):
        response = getattr(failure.value, "response", None)
        status = response.status if response is not None else None
        code = (
            "download_failed"
            if status is None
            else "source_rate_limited"
            if status == 429
            else "unexpected_304"
            if status == 304
            else "http_5xx"
            if status >= 500
            else "http_error"
        )
        yield {"type": "diagnostic", "code": code}

    def parse_list(self, response):
        if response.url in self.list_urls:
            yield {"type": "diagnostic", "code": "pagination_loop"}
            return
        self.list_urls.add(response.url)
        links = response.css(self.params.get("links", ".documents a"))
        if not links:
            yield {"type": "diagnostic", "code": "list_template_changed"}
            return
        for link in links:
            href = link.attrib.get("href")
            if not href:
                yield {"type": "diagnostic", "code": "missing_document_href"}
                continue
            url = response.urljoin(href)
            yield {
                "type": "document_ref",
                "url": url,
                "snapshot_id": response.meta["seal_snapshot_id"],
            }
            yield scrapy.Request(
                url, callback=self.parse_detail, errback=self.failed, meta={"seal_role": "detail"}
            )
        pages = response.css(self.params.get("next_page", "a.next"))
        if len(pages) > 1:
            yield {"type": "diagnostic", "code": "ambiguous_pagination"}
        for page in pages:
            url = response.urljoin(page.attrib["href"])
            if url in self.list_urls:
                yield {"type": "diagnostic", "code": "pagination_loop"}
            else:
                yield scrapy.Request(
                    url, callback=self.parse_list, errback=self.failed, meta={"seal_role": "list"}
                )

    def parse_detail(self, response):
        content_type = response.headers.get("Content-Type", b"").decode().lower()
        item = {
            "type": "candidate",
            "url": response.url,
            "snapshot_id": response.meta["seal_snapshot_id"],
            "observation_id": response.meta["seal_observation_id"],
            "date": None,
            "attachments": [],
            "locators": {},
        }
        if "application/pdf" in content_type:
            reader = PdfReader(BytesIO(response.body), strict=True)
            texts = [(page.extract_text() or "").strip() for page in reader.pages]
            if not texts or any(not text for text in texts):
                yield {"type": "diagnostic", "code": "pdf_text_layer_required"}
                return
            item.update(title=texts[0].splitlines()[0], body="\n".join(texts))
            item["locators"] = {
                "title": {"kind": "pdf", "page": 1, "start": 0, "end": len(item["title"])},
                "body": {
                    "kind": "segments",
                    "segments": [
                        {"kind": "pdf", "page": i, "start": 0, "end": len(text)}
                        for i, text in enumerate(texts, 1)
                    ],
                },
            }
        elif "application/json" in content_type:
            data = json.loads(response.body)
            for field in ("title", "body", "date"):
                item[field] = data.get(field)
                if item[field] is not None:
                    item["locators"][field] = {"kind": "json", "pointer": "/" + field}
        elif "text/plain" in content_type:
            text = response.text
            title, _, body = text.partition("\n")
            item.update(title=title.strip(), body=body.strip())
            item["locators"] = {
                "title": {"kind": "text", "start": 0, "end": len(title)},
                "body": {"kind": "text", "start": len(title) + 1, "end": len(text)},
            }
        elif "text/html" in content_type:
            params = self.params
            for field, selector in {
                "title": params.get("title", "h1"),
                "body": params.get("body", "article"),
                "date": params.get("date", "time"),
            }.items():
                nodes = response.css(selector)
                if field == "date" and not nodes:
                    continue
                if len(nodes) != 1:
                    yield {"type": "diagnostic", "code": "ambiguous_or_missing_field"}
                    return
                item[field] = " ".join(nodes[0].xpath(".//text()").getall()).strip()
                item["locators"][field] = {
                    "kind": "xpath",
                    "path": nodes[0].root.getroottree().getpath(nodes[0].root),
                }
            item["attachments"] = [
                {"url": response.urljoin(href), "status": "not_fetched"}
                for href in response.css("a.attachment::attr(href)").getall()
            ]
        else:
            yield {"type": "diagnostic", "code": "unsupported_content_type"}
            return
        yield item
