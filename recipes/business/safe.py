"""Beijing penalties: declared iframe, all public pages and every listed decision."""

from w3lib.url import add_or_replace_parameter

from seal.core import SealError


class Safe:
    def __init__(self, spider):
        self.spider = spider
        self.pages, self.keys, self.total_pages = set(), set(), None

    def parse(self, response, parent=None):
        if response.meta.get("seal_role") == "detail":
            return
        if "/beijing/xzcfxxgs/" in response.url:
            frames = response.css('iframe[src*="/www/illegal?siteid=beijing"]')
            if len(frames) != 1:
                raise SealError("business_iframe_missing")
            yield self.spider.next(response, frames[0].attrib["src"], "iframe")
            return
        if "/www/illegal/listQuery" in response.url:
            return
        current, total = response.css("#page::text").get(), response.css("#total::text").get()
        if not current or not total or not current.isdigit() or not total.isdigit():
            raise SealError("pagination_contract_missing")
        page, count = int(current), int(total)
        if page in self.pages or not 1 <= page <= count:
            raise SealError("pagination_loop")
        if self.total_pages is not None and self.total_pages != count:
            raise SealError("pagination_total_changed")
        self.total_pages = count
        self.pages.add(page)
        rows = response.css(".cfxx_jg table tr:has(td)")
        if not rows:
            raise SealError("business_list_template_changed")
        keys = [n.attrib["href"] for n in response.css(".cfxx_jg table td a[href]")]
        if len(set(keys)) != len(keys) or set(keys) & self.keys:
            raise SealError("duplicate_detail_url")
        self.keys.update(keys)
        if not self.spider.expansion_enabled:
            return
        links = response.css(".cfxx_jg table td a[href]")
        if len(links) != len(rows):
            raise SealError("business_detail_missing")
        for link in links:
            yield self.spider.next(response, link.attrib["href"], "detail")
        self.spider.crawler.stats.set_value("seal/business_pages", len(self.pages))
        self.spider.crawler.stats.set_value("seal/business_expected_pages", count)
        self.spider.crawler.stats.set_value("seal/business_discovered_decisions", len(self.keys))
        if page < count:
            if "/www/illegal/index?" not in response.text:
                raise SealError("pagination_contract_missing")
            url = add_or_replace_parameter(
                response.urljoin("/www/illegal/index?siteid=beijing"), "page", str(page + 1)
            )
            yield self.spider.next(response, url, "iframe")
        elif len(self.pages) != count:
            raise SealError("pagination_incomplete")
