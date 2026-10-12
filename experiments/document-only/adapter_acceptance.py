"""Execute every retained adapter through the same real CLI and persistence path."""

import yaml
from adapter_fixture import PERMIT
from harness import BASE


def document_count(h, source):
    return h.sql(
        "SELECT count(*) AS n FROM seal.document WHERE source_id=%s AND latest_document_version IS NOT NULL",
        (source,),
    )[0]["n"]


def exercise(h):
    for name, recipe, path, params, count in yaml.safe_load(
        (BASE / "config/cases.yaml").read_text()
    )["cases"]:
        config = h.config(name, [path], recipe=recipe, params=params)
        run = h.cli("collect", config)
        h.check(name + "_complete", run["status"], "complete")
        h.check(name + "_documents", document_count(h, name), count)
        recheck = h.cli("recheck", config)
        h.check(name + "_unchanged", recheck["summary"]["counts"]["unchanged"], count)
    search = h.site.url + "/legal/search"
    config = h.config(
        "legal-form",
        ["/legal/search"],
        recipe="vietnam_legal",
        params={"search": {"url": search, "year": 2026, "category_ids": ["1"]}},
        methods=["GET", "HEAD", "POST"],
        public_html_forms=[
            {
                "url": search,
                "parent_url": search,
                "form_xpath": '//form[@id="form1"]',
                "fields": [
                    "__VIEWSTATE",
                    "__EVENTVALIDATION",
                    "ctrl_191017_163$drdDocCategory",
                    "ctrl_191017_163$drdDocYear",
                    "ctrl_191017_163$btnSearch",
                ],
                "select_fields": ["ctrl_191017_163$drdDocCategory", "ctrl_191017_163$drdDocYear"],
                "submit": {"name": "ctrl_191017_163$btnSearch", "value": "Search"},
            }
        ],
    )
    first = h.cli("collect", config)
    h.check("form_documents", document_count(h, "legal-form"), 4)
    recheck = h.cli("recheck", config)
    h.check("fresh_form_recheck_complete", recheck["status"], "complete")
    h.check("form_identity_stable", document_count(h, "legal-form"), 4)
    before = h.sql("SELECT inputs FROM seal.run WHERE id=%s", (first["run_id"],))[0]["inputs"]
    after = h.sql("SELECT inputs FROM seal.run WHERE id=%s", (recheck["run_id"],))[0]["inputs"]
    left = next(i for i in before if i["method"] == "POST")
    right = next(i for i in after if i["method"] == "POST")
    h.check("fresh_form_body_changed", left["request_body_sha256"] != right["request_body_sha256"])
    h.check("fresh_form_resource_stable", left["request_identity"], right["request_identity"])
    fields = {
        "page.pageNo": "page",
        "page.orderBy": "empty",
        "page.order": "empty",
        "tempReportKey": "query_id",
        "province": "region",
        "city": "region",
        "management": "management",
        "registerentername": "empty",
        "xkznum": "empty",
        "treadname": "empty",
        "treadcode": "empty",
        "publishtime": "empty",
    }
    config = h.config(
        "permit-form",
        [PERMIT],
        recipe="business",
        params={
            "source": "dd-357",
            "permit_stage": "collect",
            "province": "110000000000",
            "first_page": 1,
            "last_page": 2,
        },
        methods=["GET", "HEAD", "POST"],
        public_post_rules=[{"url": h.site.url + PERMIT, "fields": fields}],
    )
    h.cli("collect", config)
    h.check("permit_pages_distinct", document_count(h, "permit-form"), 5)
    h.cli("recheck", config)
    h.check("permit_nonce_identity_stable", document_count(h, "permit-form"), 5)
    h.capture(
        "adapter-documents",
        h.sql("""SELECT d.source_id,d.url,v.source_format,v.content FROM seal.document d
        JOIN seal.document_version v ON v.id=d.latest_document_version WHERE d.source_id NOT IN ('all','failures') ORDER BY d.source_id,d.url"""),
    )
