"""The original national college notice and all its displayed XLS workbooks."""

from seal.core import SealError
from seal.helpers import input_reference, is_attachment_response


class Education:
    def __init__(self, spider):
        self.spider = spider

    def parse(self, response, parent=None):
        if is_attachment_response(response):
            return
        if not self.spider.expansion_enabled:
            return
        links = response.css(".TRS_Editor a[href]")
        files = {
            response.urljoin(n.attrib["href"])
            for n in links
            if n.attrib["href"].lower().endswith(".xls")
        }
        if not files:
            raise SealError("business_attachment_missing")
        self.spider.crawler.stats.set_value("seal/business_expected_workbooks", len(files))
        for url in sorted(files):
            yield self.spider.next(response, url, "attachment", parent=input_reference(response))

    def parse_options(self, response):
        return (
            {}
            if is_attachment_response(response)
            else {"title_css": "h1", "body_css": ".TRS_Editor"}
        )
