"""Public permit form: region coverage planning and bounded resumable page segments."""

import re

import scrapy

from seal.core import SealError
from seal.helpers import html_record, node_text, record_item, table_records, xpath_locator

LIST = "/perxxgkinfo/syssb/xkgg/xkgg!licenseInformation.action"
REGIONS = "/perxxgkinfo/syssb/xkgg/xkgg!getRegions.action"
FIELDS = {
    "page.pageNo",
    "page.orderBy",
    "page.order",
    "tempReportKey",
    "province",
    "city",
    "management",
    "registerentername",
    "xkznum",
    "treadname",
    "treadcode",
    "publishtime",
}


class Permit:
    def __init__(self, spider):
        self.spider = spider
        self.stage = spider.params.get("permit_stage", "plan")
        self.pending, self.partitions, self.pages, self.keys = [], [], set(), set()
        self.total = None
        self.last_form = None

    def form(self, response, region, page, *, phase="page"):
        form = response.css("#mainForm")
        if len(form) != 1 or response.urljoin(form.attrib["action"]) != response.urljoin(LIST):
            raise SealError("public_form_declaration_changed")
        names = {n.attrib["name"] for n in form.css("[name]") if n.attrib["name"]}
        if names != FIELDS:
            raise SealError("public_form_declaration_changed")
        nonce = form.css('input[name="tempReportKey"]::attr(value)').get()
        if not nonce or not re.fullmatch("[0-9a-f]{32}", nonce):
            raise SealError("public_form_query_id_missing")
        values = dict.fromkeys(sorted(FIELDS), "")
        values.update({"page.pageNo": str(page), "tempReportKey": nonce, **region})
        request = self.spider.next(response, LIST, "api")
        request = request.replace(
            method="POST",
            body=scrapy.FormRequest(response.urljoin(LIST), formdata=values).body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": response.url},
        )
        request.meta.update({"permit_region": region, "permit_page": page, "permit_phase": phase})
        return request

    def fresh(self, response, region):
        # Serial region queries retain the currently returned public query form.
        # A direct repeat GET is rejected by this site's own navigation guard.
        return self.form(self.last_form, region, 1, phase="filter")

    def regions(self, response, parent, region=None):
        if REGIONS not in response.text:
            raise SealError("public_regions_declaration_changed")
        request = self.spider.next(response, REGIONS, "api")
        request = request.replace(
            method="POST",
            body=scrapy.FormRequest(
                response.urljoin(REGIONS), formdata={"parentCode": parent}
            ).body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": response.url},
        )
        request.meta["permit_parent"] = parent
        request.meta["permit_region"] = region
        return request

    def complete_partition(self, response, region, pages, last_rows):
        total = (pages - 1) * 10 + last_rows
        self.partitions.append({**region, "pages": pages, "total": total})
        self.spider.crawler.stats.set_value("seal/permit_partitions", self.partitions.copy())
        if self.pending:
            yield self.fresh(response, self.pending.pop(0))

    def parse(self, response, parent=None):
        if "xkgk=getxxgkContent" in response.url:
            if not response.css(".tab0") or not response.css("#apply_table"):
                raise SealError("business_detail_missing")
            yield html_record(
                response,
                title="head title",
                body="body",
                date=None,
                record_type="environmental_permit_detail",
            )
            return
        if REGIONS in response.url:
            payload = response.json()
            rows = payload.get("regions")
            if type(rows) is not list or not rows:
                raise SealError("public_regions_missing")
            parent_code = response.meta["permit_parent"]
            codes = [r["regioncode"] for r in rows]
            if len(set(codes)) != len(codes) or any(
                not re.fullmatch("[0-9]{12}", c) for c in codes
            ):
                raise SealError("public_regions_invalid")
            if any(r["parentcode"] != parent_code for r in rows):
                raise SealError("public_regions_parent_mismatch")
            if parent_code == "000000000000":
                self.spider.crawler.stats.set_value("seal/permit_provinces", rows)
                self.pending.extend({"province": c, "city": "", "management": ""} for c in codes)
            else:
                self.pending[0:0] = [
                    {"province": parent_code, "city": c, "management": ""} for c in codes
                ]
            for index, row in enumerate(rows):
                locators = {k: {"kind": "json", "pointer": f"/regions/{index}/{k}"} for k in row}
                yield record_item(
                    response,
                    record_type="environmental_permit_region",
                    record_key=row["regioncode"],
                    data=row,
                    locators=locators,
                    key_locator=locators["regioncode"],
                )
            if self.spider.expansion_enabled and self.stage != "regions":
                yield self.fresh(response, self.pending.pop(0))
            return
        if LIST not in response.url:
            if "/perxxgkinfo/errorinfo.jsp" in response.url:
                expected = b"https://permit.mee.gov.cn/permitExt/defaults/default-index!getInformation.action"
                self.spider.crawler.stats.set_value(
                    "seal/permit_public_referer_matches",
                    response.request.headers.get("Referer") == expected,
                )
            links = response.css('a[href*="xkgg!licenseInformation.action"]')
            urls = {response.urljoin(n.attrib["href"]) for n in links}
            if len(urls) != 1:
                raise SealError("business_permit_entry_missing")
            yield self.spider.next(response, urls.pop(), "list")
            return
        self.last_form = response
        phase = response.meta.get("permit_phase")
        region = response.meta.get("permit_region")
        if phase == "fresh":
            yield self.form(response, region, 1, phase="filter")
            return
        if region is None and self.spider.expansion_enabled:
            if (
                self.stage == "regions"
                or self.stage == "plan"
                and not self.spider.params.get("province")
            ):
                yield self.regions(response, "000000000000")
            else:
                region = {
                    k: self.spider.params.get(k, "") for k in ("province", "city", "management")
                }
                yield self.form(response, region, 1, phase="filter")
            return
        current = response.css("#pageNo::attr(value)").get()
        total_match = re.search(r"共\s*([0-9]+)\s*页", response.text)
        if total_match is None:
            raise SealError("pagination_contract_missing")
        pages = int(total_match[1])
        if not current or not current.isdigit():
            raise SealError("pagination_contract_missing")
        page = int(current)
        requested = response.meta.get("permit_page")
        if requested is None and response.request.method == "POST":
            match = re.search(
                r"(?:^|&)page\.pageNo=([0-9]+)", response.request.body.decode("ascii")
            )
            requested = int(match[1]) if match else None
        if requested is not None and requested != page:
            raise SealError("pagination_page_mismatch")
        rows = response.css(".tabtd tr:not(.tbhead)")
        if len(rows) > 10 or any(len(r.css("td")) != 9 for r in rows):
            raise SealError("business_list_template_changed")
        if self.stage == "plan" and self.spider.expansion_enabled:
            if pages > 3000:
                self.spider.crawler.stats.set_value("seal/permit_split_required", region)
                if not region["city"]:
                    yield self.regions(response, region["province"], region)
                elif not region["management"]:
                    self.pending[0:0] = [
                        {**region, "management": "1"},
                        {**region, "management": "0"},
                    ]
                    yield self.fresh(response, self.pending.pop(0))
                else:
                    raise SealError("public_partition_still_exceeds_limit")
            elif page != pages:
                yield self.form(response, region, pages, phase="tail")
            else:
                if rows:
                    key_node = rows[-1].css("td")[2]
                    locator = {
                        "kind": "text",
                        "start": total_match.start(1),
                        "end": total_match.end(1),
                    }
                    yield record_item(
                        response,
                        record_type="environmental_permit_partition",
                        record_key=node_text(key_node),
                        data={"pages": str(pages)},
                        locators={"pages": locator},
                        key_locator=xpath_locator(key_node),
                    )
                else:
                    raise SealError("public_partition_empty_unproven")
                yield from self.complete_partition(response, region, pages, len(rows))
            return
        if pages > 3000:
            raise SealError("public_form_partition_required")
        if self.total is not None and self.total != pages:
            raise SealError("pagination_total_changed")
        self.total = pages
        self.spider.crawler.stats.set_value("seal/permit_partition_pages", pages)
        first = self.spider.params.get("first_page", 1)
        last = min(self.spider.params.get("last_page", pages), pages)
        if phase == "filter" and first > 1:
            yield self.form(response, region, first)
            return
        if page in self.pages:
            raise SealError("pagination_loop")
        self.pages.add(page)
        keys = [node_text(r.css("td")[2]) for r in rows]
        if len(set(keys)) != len(keys) or set(keys) & self.keys:
            raise SealError("duplicate_business_key")
        self.keys.update(keys)
        yield from table_records(
            response,
            rows=".tabtd tr:not(.tbhead)",
            fields={
                name: f"td:nth-child({i})"
                for i, name in enumerate(
                    [
                        "province",
                        "city",
                        "license_number",
                        "company_name",
                        "industry",
                        "validity",
                        "issue_date",
                        "management",
                    ],
                    1,
                )
            },
            key_field="license_number",
            record_type="environmental_permit_row",
        )
        if not self.spider.expansion_enabled:
            return
        links = response.css(".tabtd tr:not(.tbhead) td a[href]")
        if len(links) != len(rows):
            raise SealError("business_detail_missing")
        for link in links:
            yield self.spider.next(response, link.attrib["href"], "detail")
        self.spider.crawler.stats.set_value("seal/permit_segment_pages", sorted(self.pages))
        self.spider.crawler.stats.set_value("seal/permit_segment_keys", len(self.keys))
        if page < last:
            yield self.form(response, region, page + 1)
        elif sorted(self.pages) != list(range(first, last + 1)):
            raise SealError("pagination_incomplete")
