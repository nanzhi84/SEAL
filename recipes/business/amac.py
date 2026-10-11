"""Discover displayed public directory APIs, then enumerate their native total."""

import re

from w3lib.url import add_or_replace_parameter

from seal.core import SealError
from seal.helpers import record_item
from seal.record_validation import json_pointer

SPECS = {
    "dd-250": (
        "/portal/front/financial/fundTrustee/findFundTrusteesPage",
        "trustName",
        ("trustName", "regAddr"),
        "mutual_fund_trustee",
    ),
    "dd-247": (
        "/portal/front/mutualFund/findMutualFundHousePage",
        "houseName",
        ("houseName", "registerAddr", "officeAddr", "website", "phone"),
        "mutual_fund_manager",
    ),
}


class Amac:
    def __init__(self, spider):
        self.spider = spider
        self.api, self.key, self.fields, self.record_type = SPECS[spider.params["source"]]
        self.total, self.pages, self.names = None, set(), set()

    def parse(self, response, parent=None):
        if self.api not in response.url:
            # The endpoint and empty filter contract are confirmed in the actual page.
            if not re.search(r"url\s*:\s*URL\s*\+\s*['\"]" + re.escape(self.api), response.text):
                raise SealError("business_api_declaration_changed")
            for field in ("pageNo", "pageSize"):
                if field + ":" not in response.text:
                    raise SealError("pagination_contract_missing")
            url = self.api + "?pageNo=1&pageSize=10"
            if self.key == "houseName":
                url += "&houseName=&registerAddr=&officeAddr="
            yield self.spider.next(response, url, "api")
            return
        page_match = re.search(r"[?&]pageNo=([1-9][0-9]*)", response.url)
        if page_match is None:
            raise SealError("pagination_contract_missing")
        page = int(page_match[1])
        envelope = json_pointer(response.body, "")
        if envelope.get("code") != 200 or envelope.get("data", {}).get("errcode") != 0:
            raise SealError("api_envelope_not_success")
        payload = envelope["data"]["data"]
        total, rows = payload["total"], payload["dataList"]
        if type(total) is not int or total < 0 or type(rows) is not list:
            raise SealError("pagination_contract_missing")
        if self.total is None:
            self.total = total
            self.spider.crawler.stats.set_value("seal/business_expected_records", total)
        if self.total != total:
            raise SealError("pagination_total_changed")
        pages = max(1, (total + 9) // 10)
        if page in self.pages or not 1 <= page <= pages:
            raise SealError("pagination_loop")
        if len(rows) != min(10, max(0, total - 10 * (page - 1))):
            raise SealError("pagination_incomplete")
        names = [row[self.key] for row in rows]
        if any(type(name) is not str or not name.strip() for name in names):
            raise SealError("record_key_missing")
        if len(set(names)) != len(names) or set(names) & self.names:
            raise SealError("duplicate_business_key")
        self.pages.add(page)
        self.names.update(names)
        self.spider.crawler.stats.set_value("seal/business_pages", len(self.pages))
        for index, row in enumerate(rows):
            data = {field: row[field] for field in self.fields}
            locators = {
                field: {"kind": "json", "pointer": f"/data/data/dataList/{index}/{field}"}
                for field in data
            }
            yield record_item(
                response,
                record_type=self.record_type,
                record_key=row[self.key],
                data=data,
                locators=locators,
                key_locator=locators[self.key].copy(),
            )
        if not self.spider.expansion_enabled:
            return
        if page < pages:
            yield self.spider.next(
                response, add_or_replace_parameter(response.url, "pageNo", str(page + 1)), "api"
            )
        elif len(self.pages) != pages or len(self.names) != total:
            raise SealError("pagination_incomplete")
