"""Full guiding-case publication lists and each linked article, no frozen sample."""

import re

from seal.core import SealError
from seal.helpers import (
    input_reference,
    is_attachment_response,
    node_text,
)


class Publications:
    def __init__(self, spider):
        self.spider, self.source = spider, spider.params["source"]
        self.pages, self.details, self.page_count = set(), set(), None

    def parse(self, response, parent=None):
        if is_attachment_response(response):
            return
        if response.meta.get("seal_role") == "detail":
            if self.spider.expansion_enabled:
                body_links = response.css(
                    ".detail .txt_txt a[href]" if self.source == "dd-017" else "#fontzoom a[href]"
                )
                for link in body_links:
                    if re.search(
                        r"\.(?:pdf|xls|xlsx|doc|docx|txt)(?:[?#]|$)", link.attrib["href"], re.I
                    ):
                        yield self.spider.next(
                            response,
                            link.attrib["href"],
                            "attachment",
                            parent=input_reference(response),
                        )
            return
        if not self.spider.expansion_enabled:
            return
        if response.url in self.pages:
            raise SealError("pagination_loop")
        self.pages.add(response.url)
        selector = (
            '.sec_list a[href*="/shenpan/xiangqing/"]'
            if self.source == "dd-017"
            else ".commonList_con .li_line a[href]"
        )
        links = response.css(selector)
        if not links:
            raise SealError("business_list_template_changed")
        for link in links:
            url = response.urljoin(link.attrib["href"])
            if url not in self.details:
                self.details.add(url)
                yield self.spider.next(response, url, "detail")
        self.spider.crawler.stats.set_value("seal/business_discovered_details", len(self.details))
        self.spider.crawler.stats.set_value("seal/business_pages", len(self.pages))
        next_url = None
        if self.source == "dd-017":
            current_match = re.search(r"/77(?:_([0-9]+))?\.html$", response.url)
            if not current_match:
                raise SealError("pagination_contract_missing")
            current = int(current_match[1] or "1")
            tail = [a.attrib["href"] for a in response.css(".page a") if node_text(a) == "尾页"]
            if len(tail) != 1:
                raise SealError("pagination_contract_missing")
            tail_match = re.search(r"/77(?:_([0-9]+))?\.html$", tail[0])
            if not tail_match:
                raise SealError("pagination_contract_missing")
            count = int(tail_match[1] or "1")
            next_links = [
                a.attrib["href"] for a in response.css(".page a") if node_text(a) == "下一页"
            ]
            if current < count:
                if len(next_links) != 1 or not next_links[0].endswith(f"/77_{current + 1}.html"):
                    raise SealError("pagination_contract_missing")
                next_url = next_links[0]
        else:
            declarations = re.findall(
                r"createPageHTML\('page_div',\s*(\d+),\s*(\d+),\s*'index',\s*'shtml',\s*(\d+)\)",
                response.text,
            )
            if len(declarations) != 1:
                raise SealError("pagination_contract_missing")
            count, current, total = map(int, declarations[0])
            self.spider.crawler.stats.set_value("seal/business_expected_links", total)
            if current < count:
                next_url = f"index_{current + 1}.shtml"
            elif len(self.details) != total:
                raise SealError("pagination_incomplete")
        if not 1 <= current <= count or (self.page_count is not None and count != self.page_count):
            raise SealError("pagination_total_changed")
        self.page_count = count
        self.spider.crawler.stats.set_value("seal/business_expected_pages", count)
        if next_url:
            yield self.spider.next(response, next_url, "list")
        elif len(self.pages) != count:
            raise SealError("pagination_incomplete")

    def parse_options(self, response):
        if response.meta.get("seal_role") != "detail" or is_attachment_response(response):
            return {}
        if self.source == "dd-017":
            return {"title_css": ".detail > .title", "body_css": ".detail .txt_txt"}
        return {"body_css": "#fontzoom"}
