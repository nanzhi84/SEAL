"""Reviewed official detail metadata; classification requires positive archived membership."""

import re
from datetime import datetime

import scrapy

from seal.core import SealError
from seal.helpers import input_reference, record_item, xpath_locator

KEYS = ("Lĩnh vực:", "Cơ quan ban hành:", "Năm ban hành:")


def issued_year(value, *, format):
    return datetime.strptime(value, format).year


def subject_list(value):
    if isinstance(value, list):
        return value if value else None
    return [value] if value.strip() else None


class VietnamLegalSpider(scrapy.Spider):
    name = "vietnam_legal_metadata"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.details, self.memberships = [], []
        self.pending = (
            len(context["seeds"]) + len(params.get("membership_urls", [])) + int("search" in params)
        )

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(
                seed["url"],
                callback=self.parse_detail,
                errback=self.failed,
                meta={"seal_role": "detail"},
            )
        for url in self.params.get("membership_urls", []):
            yield scrapy.Request(
                url, callback=self.parse_membership, errback=self.failed, meta={"seal_role": "list"}
            )

        if "search" in self.params:
            yield scrapy.Request(
                self.params["search"]["url"],
                callback=self.parse_form,
                errback=self.failed,
                meta={"seal_role": "list"},
            )

    def parse_form(self, response):
        search = self.params["search"]
        self.pending -= 1
        self.pending += len(search["category_ids"])
        for category in search["category_ids"]:
            data = {
                "ctrl_191017_163$drdDocCategory": category,
                "ctrl_191017_163$drdDocYear": str(search["year"]),
            }
            meta = {
                "seal_role": "list",
                "seal_parent_url": response.url,
                "seal_parent_snapshot_id": response.meta["seal_snapshot_id"],
                "seal_parent_observation_id": response.meta["seal_observation_id"],
            }
            yield scrapy.FormRequest.from_response(
                response,
                formxpath="//form[@id='form1']",
                formdata=data,
                clickdata={"name": "ctrl_191017_163$btnSearch"},
                callback=self.parse_membership,
                errback=self.failed,
                meta=meta,
            )

    def failed(self, failure):
        self.pending -= 1
        yield {"type": "diagnostic", "code": "download_failed"}

    def parse_detail(self, response):
        self.details.append(response)
        self.pending -= 1
        if self.pending == 0:
            yield from self.emit()

    def parse_membership(self, response):
        self.memberships.append(response)
        self.pending -= 1
        if self.pending == 0:
            yield from self.emit()

    def field(self, response, label):
        nodes = response.xpath(
            "//div[contains(@class,'Detail')]//tr/td[1][normalize-space(.)='"
            + label
            + "']/following-sibling::td[1]"
        )
        if len(nodes) != 1:
            return None, None
        value = " ".join(nodes[0].xpath(".//text()").getall()).strip()
        return (value, xpath_locator(nodes[0])) if value else (None, None)

    def emit(self):
        contract = self.context["field_contract"]
        for response in self.details:
            number, key = self.field(response, "Số ký hiệu")
            if not number:
                raise SealError("record_key_missing")
            authority, authority_locator = self.field(response, "Cơ quan ban hành")
            raw_date, year_locator = self.field(response, "Ngày ban hành")
            parser = contract["field_parsers"][KEYS[2]]
            try:
                year = issued_year(raw_date, **parser["params"]) if raw_date else None
            except (ValueError, TypeError, OverflowError):
                year = None
            subjects, subject_locator = self.field(response, "Lĩnh vực")
            subjects = subject_list(subjects) if subjects else None
            category_values, category_locators = [], []
            match = re.search(r"(?:[?&]docid=|/detail/)([0-9]+)", response.url)
            docid = match.group(1) if match else None
            supplementary, supporting = [], {}
            for page in self.memberships:
                if docid is None or not page.xpath("//a[contains(@href,'docid=" + docid + "')]"):
                    continue
                ref = input_reference(page)
                supplementary.append(
                    {"snapshot_id": ref["snapshot_id"], "observation_id": ref["observation_id"]}
                )
                year_node = page.xpath("//select[contains(@id,'drdDocYear')]/option[@selected]")
                if len(year_node) == 1:
                    found = " ".join(year_node[0].xpath(".//text()").getall()).strip()
                    if year is not None and found != str(year):
                        year, year_locator = None, None
                    elif year is not None:
                        supporting.setdefault(KEYS[2], []).append(
                            {
                                **xpath_locator(year_node[0]),
                                "snapshot_id": ref["snapshot_id"],
                                "value": found,
                            }
                        )
                category = page.xpath(
                    "//select[contains(@id,'drdDocCategory')]/option[@selected and @value!='0']"
                )
                if subjects is None and len(category) == 1:
                    label = " ".join(category[0].xpath(".//text()").getall()).strip()
                    if label:
                        if label in category_values:
                            continue
                        category_values.append(label)
                        # Membership is part of the locator predicate: a selected category
                        # without this exact public document link cannot justify the value.
                        category_locators.append(
                            {
                                "kind": "xpath",
                                "path": "//select[contains(@id,'drdDocCategory')]/option[@selected and @value!='0'][ancestor::form//a[contains(@href,'docid="
                                + docid
                                + "')]]",
                                "snapshot_id": ref["snapshot_id"],
                            }
                        )
            if subjects is None and category_values:
                subjects = category_values
                subject_locator = {"kind": "values", "locators": category_locators}
            data = dict(zip(KEYS, (subjects, authority, year), strict=True))
            locators = {}
            for field, value, locator in zip(
                KEYS,
                (subjects, authority, year),
                (subject_locator, authority_locator, year_locator),
                strict=True,
            ):
                if value is not None:
                    locators[field] = {
                        **locator,
                        **(
                            {"parser": contract["field_parsers"][field]}
                            if field in contract["field_parsers"]
                            else {}
                        ),
                    }
            item = record_item(
                response,
                record_type="legal_document",
                record_key=number,
                data=data,
                locators=locators,
                key_locator=key,
                detail_url=response.url,
                supplementary_inputs=supplementary,
            )
            item["data_schema_id"] = contract["data_schema"]["$id"]
            if supporting:
                item["supporting_locators"] = supporting
            yield item
