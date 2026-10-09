"""Reviewed public HTML adapters with an explicit two-page business scope.

The bounded scope selects frozen detail URLs only when their real list links
are observed. Pagination follows inspected site declarations, never executed
JavaScript. The SAFE adapter retains public table rows by decision number and
one linked decision; form submissions and anonymous session URLs are not used.
"""

import re

import scrapy

from seal.helpers import diagnostic, follow, html_record, node_text, static_resources, table_records

DETAIL_SELECTORS = {
    "dd-017": {
        "title": ".detail > .title",
        "body": ".detail .txt_txt",
        "date": ".detail_mes .message li:nth-child(2)",
        "record_type": "court_guiding_case",
    },
    "dd-041": {
        "title": ".detail_tit",
        "body": "#fontzoom",
        "date": ".detail_extend1",
        "record_type": "procuratorate_guiding_cases",
    },
    "dd-116": {
        "title": ".title_cen h2",
        "body": "#zoom",
        "date": ".con_div .time",
        "record_type": "market_supervision_notice",
    },
    "dd-102": {
        "title": ".cfxx_jg > .red_tx",
        "body": ".cfxx_jg table",
        "date": ".cfxx_jg table tr:nth-child(12) td",
        "record_type": "foreign_exchange_penalty_detail",
    },
}
LIST_SELECTORS = {
    "dd-017": '.sec_list a[href*="/shenpan/xiangqing/"]',
    "dd-041": ".commonList_con .li_line a[href]",
    "dd-116": ".list-right_title a[href]",
}


class LiveASpider(scrapy.Spider):
    name = "v12_live_public_html"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.source = params["source"]
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.pages = set()
        self.fixed_details = set(params["fixed_details"])

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
        if response.meta.get("seal_role") == "detail":
            yield html_record(response, **DETAIL_SELECTORS[self.source])
            return
        if self.source == "dd-102":
            yield from self.safe(response)
            return
        if response.url in self.pages:
            yield diagnostic(response, "pagination_loop")
            return
        self.pages.add(response.url)
        links = response.css(LIST_SELECTORS[self.source])
        if not links:
            yield diagnostic(response, "list_template_changed")
            return
        selected = [a for a in links if response.urljoin(a.attrib["href"]) in self.fixed_details]
        if not selected:
            yield diagnostic(response, "missing_document_href")
        for link in selected:
            yield follow(response, link.attrib["href"], "detail", self.parse, self.failed)
        if len(self.pages) >= self.params.get("max_pages", 2):
            return
        next_url = self.next_html_page(response)
        if next_url:
            yield follow(response, next_url, "list", self.parse, self.failed)
        else:
            yield diagnostic(response, "ambiguous_pagination")

    def next_html_page(self, response):
        if self.source == "dd-017":
            links = [
                a.attrib.get("href") for a in response.css(".page a") if node_text(a) == "下一页"
            ]
            return links[0] if len(links) == 1 else None
        if self.source == "dd-041":
            declarations = re.findall(
                r"createPageHTML\('page_div',\s*(\d+),\s*(\d+),\s*'index',\s*'shtml',\s*\d+\)",
                response.text,
            )
            if len(declarations) == 1:
                count, current = map(int, declarations[0])
                if 1 <= current < count:
                    return f"index_{current + 1}.shtml"
        if self.source == "dd-116":
            declarations = re.findall(
                r'createPageHTML\(\s*(\d+)\s*,\s*(\d+)\s*,\s*"index"\s*,\s*"shtml"\s*\)',
                response.text,
            )
            if len(declarations) == 1:
                count, current = map(int, declarations[0])
                if 0 <= current < count - 1:
                    return f"index_{current + 1}.shtml"
        return None

    def safe(self, response):
        if response.meta.get("seal_role") == "list":
            frames = response.css('iframe[src="/www/illegal?siteid=beijing"]')
            if len(frames) != 1:
                yield diagnostic(response, "ambiguous_or_missing_field")
                return
            yield from static_resources(
                response,
                iframe_callback=self.parse,
                attachment_callback=self.parse,
                errback=self.failed,
                iframe_selector='iframe[src="/www/illegal?siteid=beijing"]',
                attachment_selector=None,
            )
            return
        if response.url in self.pages:
            yield diagnostic(response, "pagination_loop")
            return
        self.pages.add(response.url)
        current, total = response.css("#page::text").get(), response.css("#total::text").get()
        limit = self.params.get("max_pages", 2)
        if (
            not current
            or not total
            or not current.isdigit()
            or not total.isdigit()
            or not 1 <= int(current) <= min(int(total), limit)
        ):
            yield diagnostic(response, "ambiguous_pagination")
            return
        rows = response.css(".cfxx_jg table tr:has(td)")
        if not rows:
            yield diagnostic(response, "list_template_changed")
            return
        yield from table_records(
            response,
            rows=".cfxx_jg table tr:has(td)",
            fields={
                "name": "td:nth-child(2)",
                "date": "td:nth-child(3)",
                "decision_number": "td:nth-child(4)",
            },
            key_field="decision_number",
            record_type="foreign_exchange_penalty_row",
        )
        for link in response.css(".cfxx_jg table td a[href]"):
            if response.urljoin(link.attrib["href"]) in self.fixed_details:
                yield follow(response, link.attrib["href"], "detail", self.parse, self.failed)
        # This exact public GET route is declared by the archived go() script.
        # Recheck may seed page 2 before page 1. The declared page number, not
        # callback order or the count of processed responses, bounds the scope.
        if int(current) < min(int(total), limit) and "/www/illegal/index?page=" in response.text:
            yield follow(
                response,
                f"/www/illegal/index?page={int(current) + 1}&siteid=beijing",
                "iframe",
                self.parse,
                self.failed,
            )
