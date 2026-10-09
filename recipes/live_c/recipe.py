"""Reviewed fixed GET samples; no arbitrary searches or company profile crawling.

AMAC's legal houseName is the natural key. lineId is deliberately omitted:
its observed stability under filtering does not establish permanent ID semantics.
Companies House records are search projections with company-number identity;
Recheck must use the same parent search, not a different company-profile schema.
"""

import re

import scrapy

from seal.core import SealError
from seal.helpers import diagnostic, follow, node_text, record_item, xpath_locator
from seal.record_validation import json_pointer

AMAC = (
    "https://www.amac.org.cn/portal/front/mutualFund/findMutualFundHousePage"
    "?pageNo={page}&pageSize=10&houseName=&registerAddr=&officeAddr="
)
COMPANIES = "https://find-and-update.company-information.service.gov.uk/search?q=TESCO"
DISPLAY_FIELDS = ("houseName", "registerAddr", "officeAddr", "website", "phone")


class LiveCSpider(scrapy.Spider):
    name = "v12_live_fixed_queries"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.allowed_domains = context["config"]["allowed_hosts"]
        self.pages = set()

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(
                seed["url"],
                callback=self.parse,
                errback=self.failed,
                meta={"seal_role": seed["role"]},
            )

    def failed(self, failure):
        response = getattr(failure.value, "response", None)
        code = "download_failed"
        if response is not None:
            code = "source_rate_limited" if response.status == 429 else "http_error"
        yield {"type": "diagnostic", "code": code}

    def parse(self, response):
        if response.url in self.pages:
            yield diagnostic(response, "pagination_loop")
            return
        self.pages.add(response.url)
        try:
            if self.params["source"] == "dd-247":
                yield from self.parse_amac(response)
            else:
                yield from self.parse_companies(response)
        except (KeyError, TypeError, ValueError, SealError) as exc:
            yield diagnostic(
                response, exc.code if isinstance(exc, SealError) else "source_schema_changed"
            )

    def parse_amac(self, response):
        page_match = re.fullmatch(re.escape(AMAC).replace(r"\{page\}", "([12])"), response.url)
        if page_match is None:
            raise SealError("unexpected_query_or_page")
        page = int(page_match[1])
        envelope = json_pointer(response.body, "")
        if envelope.get("code") != 200 or envelope.get("data", {}).get("errcode") != 0:
            raise SealError("api_envelope_not_success")
        payload = envelope["data"]["data"]
        rows, total = payload["dataList"], payload["total"]
        if type(total) is not int or total < 0 or not isinstance(rows, list):
            raise SealError("source_schema_changed")
        expected_count = min(10, max(total - (page - 1) * 10, 0))
        if not rows or len(rows) != expected_count:
            raise SealError("empty_or_oversized_list")
        names = [row.get("houseName") for row in rows if isinstance(row, dict)]
        if (
            len(names) != len(rows)
            or any(type(name) is not str or not name.strip() for name in names)
            or len(set(names)) != len(names)
        ):
            raise SealError("missing_or_nonunique_legal_name")
        for index, row in enumerate(rows):
            # Only the five fields actually rendered by the inspected source.
            # Preserve native null/string types and exact JSON Pointer evidence.
            data = {field: row[field] for field in DISPLAY_FIELDS}
            locators = {
                field: {"kind": "json", "pointer": f"/data/data/dataList/{index}/{field}"}
                for field in data
            }
            yield record_item(
                response,
                record_type="mutual_fund_manager",
                record_key=data["houseName"],
                data=data,
                locators=locators,
                key_locator=locators["houseName"].copy(),
            )
        if page < self.params["max_pages"] and page * 10 < total:
            yield follow(response, AMAC.format(page=page + 1), "api", self.parse, self.failed)

    def parse_companies(self, response):
        allowed = {COMPANIES: 1, COMPANIES + "&page=2": 2}
        if response.url not in allowed:
            raise SealError("unexpected_query_or_page")
        page = allowed[response.url]
        rows = response.css("#results li.type-company")
        if not rows:
            raise SealError("search_template_changed")
        seen = set()
        for row in rows:
            links = row.css("h3 a[href]")
            if len(links) != 1:
                raise SealError("company_link_or_number_missing")
            link = links[0]
            href = link.attrib["href"]
            match = re.fullmatch(r"/company/([A-Z0-9]{8})", href)
            if match is None or match[1] in seen:
                raise SealError("duplicate_or_missing_company_number")
            number = match[1]
            seen.add(number)
            # Exact raw attribute bytes locate the public company number. No
            # regex transform or guessed URL is introduced into Record identity.
            occurrences = list(
                re.finditer(r"href=[\"']/company/(" + re.escape(number) + r")[\"']", response.text)
            )
            if len(occurrences) != 1:
                raise SealError("ambiguous_company_number")
            number_locator = {
                "kind": "text",
                "start": occurrences[0].start(1),
                "end": occurrences[0].end(1),
            }
            paragraphs = row.css("p")
            meta = row.css("p.meta.crumbtrail:not(.inset)")
            if not paragraphs or len(meta) != 1:
                raise SealError("search_template_changed")
            data = {
                "company_number": number,
                "name": node_text(link),
                "metadata": node_text(meta[0]),
                "address": node_text(paragraphs[-1]),
            }
            locators = {
                "company_number": number_locator,
                "name": xpath_locator(link),
                "metadata": xpath_locator(meta[0]),
                "address": xpath_locator(paragraphs[-1]),
            }
            yield record_item(
                response,
                record_type="company_search_result",
                record_key=number,
                data=data,
                locators=locators,
                key_locator=number_locator.copy(),
            )
        # Outside the declared two-page sample no Request is yielded: an
        # intentional scope boundary is not a failed discovery or partial Run.
        if page < self.params["max_pages"]:
            next_links = response.css("a#next-page[href]")
            if len(next_links) != 1 or response.urljoin(next_links[0].attrib["href"]) != (
                COMPANIES + "&page=2"
            ):
                raise SealError("ambiguous_pagination")
            yield follow(response, next_links[0].attrib["href"], "list", self.parse, self.failed)
