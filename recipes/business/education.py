"""The original national college notice and all its displayed XLS workbooks."""

from seal.core import SealError
from seal.helpers import attachment_record, html_record, input_reference, is_attachment_response


class Education:
    def __init__(self, spider):
        self.spider = spider

    def parse(self, response, parent=None):
        if is_attachment_response(response):
            yield attachment_record(
                response, parent=parent, record_type="education_college_workbook"
            )
            return
        yield html_record(
            response,
            title="h1",
            body=".TRS_Editor",
            date=None,
            record_type="education_college_notice",
        )
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
