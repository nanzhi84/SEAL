"""Explicit full enumeration of AMAC's publicly displayed trustee directory.

This is a source-specific Python pagination contract, not a generic URL DSL.
Every page must agree with the frozen native total, exact page length and
Source-scoped displayed-name uniqueness. No speculative empty page is fetched.
The API has no snapshot token: this does not promise an atomic backend snapshot
or rename continuity. Missing old records retain Runtime's unknown semantics.
"""

import re

import scrapy

from seal.core import SealError
from seal.helpers import diagnostic, follow, record_item
from seal.record_validation import json_pointer

API_PATH = "/portal/front/financial/fundTrustee/findFundTrusteesPage"
PAGE_SIZE = 10


class AmacFullSpider(scrapy.Spider):
    name = "amac_trustee_full_enumeration"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.base = context["config"]["entry_urls"][0].split("?", 1)[0]
        self.pages, self.names = set(), set()

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
            yield from self.page(response)
        except (KeyError, TypeError, ValueError, SealError) as exc:
            yield diagnostic(
                response, exc.code if isinstance(exc, SealError) else "pagination_incomplete"
            )

    def page(self, response):
        if self.context["config"]["entry_urls"] != [self.base + "?pageNo=1&pageSize=10"]:
            # The full Collect contract starts at exactly page 1. A Recheck
            # may still start at any frozen parent page; its seeds are separate
            # from this immutable Source entry configuration and its target
            # scope is reported by Runtime.
            raise SealError("pagination_incomplete")
        pattern = re.escape(self.base) + r"\?pageNo=([1-9][0-9]*)&pageSize=10"
        matched = re.fullmatch(pattern, response.url)
        if not self.base.endswith(API_PATH) or matched is None:
            raise SealError("pagination_incomplete")
        page = int(matched[1])
        envelope = json_pointer(response.body, "")
        if (
            type(envelope.get("code")) is not int
            or envelope["code"] != 200
            or type(envelope.get("data", {}).get("errcode")) is not int
            or envelope["data"]["errcode"] != 0
        ):
            raise SealError("pagination_incomplete")
        payload = envelope["data"]["data"]
        total, rows = payload["total"], payload["dataList"]
        if type(total) is not int or total < 0 or not isinstance(rows, list):
            raise SealError("pagination_incomplete")
        if total != self.params["expected_total"]:
            raise SealError("pagination_total_changed")
        required_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if required_pages > min(
            self.params["max_pages"], self.context["config"]["budget"]["requests"]
        ):
            # Reject the full profile from its actual envelope before emitting
            # prefix records or creating an out-of-budget next discovery.
            raise SealError("pagination_limit")
        expected_rows = min(PAGE_SIZE, max(total - (page - 1) * PAGE_SIZE, 0))
        if not 1 <= page <= required_pages or page in self.pages or len(rows) != expected_rows:
            raise SealError("pagination_incomplete")
        names = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or type(row.get("trustName")) is not str
                or not row["trustName"].strip()
                or type(row.get("regAddr")) is not str
            ):
                raise SealError("pagination_incomplete")
            names.append(row["trustName"])
        if len(set(names)) != len(names) or set(names) & self.names:
            raise SealError("pagination_incomplete")
        self.pages.add(page)
        self.names.update(names)
        if len(self.pages) == required_pages and (
            self.pages != set(range(1, required_pages + 1)) or len(self.names) != total
        ):
            raise SealError("pagination_incomplete")
        for index, row in enumerate(rows):
            data = {field: row[field] for field in ("trustName", "regAddr")}
            locators = {
                field: {"kind": "json", "pointer": f"/data/data/dataList/{index}/{field}"}
                for field in data
            }
            yield record_item(
                response,
                record_type="mutual_fund_trustee",
                record_key=row["trustName"],
                data=data,
                locators=locators,
                key_locator=locators["trustName"].copy(),
            )
        if page < required_pages:
            # Native scheduler/fingerprint deduplication remains authoritative
            # when Recheck also has each frozen API page as a seed.
            yield follow(
                response,
                self.base + f"?pageNo={page + 1}&pageSize=10",
                "api",
                self.parse,
                self.failed,
            )
