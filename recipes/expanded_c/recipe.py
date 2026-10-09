"""Two bounded official AMAC directories with inspected JSON contracts.

Source-rendered institution names are scoped natural keys. Native numeric id is
not presented as a permanent registry identifier and is omitted from business
data. Renaming forms a new natural identity; unobserved prior records stay unknown.
"""

import re

import scrapy

from seal.core import SealError
from seal.helpers import diagnostic, follow, record_item
from seal.record_validation import json_pointer

SOURCES = {
    "dd-248": {
        "url": "https://www.amac.org.cn/portal/front/infopublic/fsAgencyAnno/findFsAgencyAnnos"
        "?pageNo={page}&pageSize=10&orgName=&regAddr=&orgType=&startTime=&endTime=",
        "key": "orgName",
        "fields": ("orgName", "regAddr", "orgType", "checkTime"),
        "record_type": "mutual_fund_sales_institution",
    },
    "dd-250": {
        "url": "https://www.amac.org.cn/portal/front/financial/fundTrustee/findFundTrusteesPage"
        "?pageNo={page}&pageSize=10",
        "key": "trustName",
        "fields": ("trustName", "regAddr"),
        "record_type": "mutual_fund_trustee",
    },
}


class ExpandedCSpider(scrapy.Spider):
    name = "expanded_amac_public_directories"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.contract = SOURCES[params["source"]]

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
        try:
            yield from self.parse_api(response)
        except (KeyError, TypeError, ValueError, SealError) as exc:
            yield diagnostic(
                response, exc.code if isinstance(exc, SealError) else "source_schema_changed"
            )

    def parse_api(self, response):
        pattern = re.escape(self.contract["url"]).replace(r"\{page\}", "([12])")
        match = re.fullmatch(pattern, response.url)
        if match is None:
            raise SealError("unexpected_fixed_query_or_page")
        page = int(match[1])
        envelope = json_pointer(response.body, "")
        if (
            type(envelope.get("code")) is not int
            or envelope["code"] != 200
            or type(envelope.get("data", {}).get("errcode")) is not int
            or envelope["data"]["errcode"] != 0
        ):
            raise SealError("api_envelope_not_success")
        payload = envelope["data"]["data"]
        rows, total = payload["dataList"], payload["total"]
        if type(total) is not int or total < 0 or not isinstance(rows, list):
            raise SealError("source_schema_changed")
        expected = min(10, max(total - (page - 1) * 10, 0))
        if not rows or len(rows) != expected:
            raise SealError("empty_or_oversized_list")
        key = self.contract["key"]
        names = [row.get(key) for row in rows if isinstance(row, dict)]
        if (
            len(names) != len(rows)
            or any(type(name) is not str or not name.strip() for name in names)
            or len(set(names)) != len(names)
        ):
            raise SealError("missing_or_nonunique_institution_name")
        for index, row in enumerate(rows):
            data = {field: row[field] for field in self.contract["fields"]}
            locators = {
                field: {"kind": "json", "pointer": f"/data/data/dataList/{index}/{field}"}
                for field in data
            }
            yield record_item(
                response,
                record_type=self.contract["record_type"],
                record_key=data[key],
                data=data,
                locators=locators,
                key_locator=locators[key].copy(),
            )
        if page < self.params["max_pages"] and page * 10 < total:
            yield follow(
                response, self.contract["url"].format(page=page + 1), "api", self.parse, self.failed
            )
