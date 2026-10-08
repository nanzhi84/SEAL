"""Site recipes for the Scrapy side (and reused on Firecrawl's rawHtml).

Each recipe is a small function `parse(selector, url, snapshot_id) -> dict`.
It receives a parsel Selector (from a Scrapy Response, or built from raw text)
so the exact same code can be replayed offline against archived bytes -- which
is what SEAL does for Replay.

Nothing here is framework magic: it is the per-source Python a maintainer writes.
"""
from __future__ import annotations

from . import extract as ex
from .extract import evidence_for, find_attachments, find_document_links, parse_date, unified


def _text(selector, css: str) -> str:
    nodes = selector.css(css)
    if not nodes:
        return ""
    return ex._clean_text(nodes[0].xpath("string(.)").get() or "")


# ---------------------------------------------------------------- A: court
def court_list(selector, url, snapshot_id):
    recs = []
    # list items: <li><a href="/shenpan/xiangqing/N.html">title</a><i class="date">d</i></li>
    for a in selector.css('a[href*="/shenpan/xiangqing/"]'):
        link = ex.absolutise(url, a.attrib.get("href", ""))
        if not link:
            continue
        date = a.xpath("following-sibling::i[contains(@class,'date')]/text()").get()
        recs.append({"url": link,
                     "text": ex._clean_text(a.xpath("string(.)").get() or ""),
                     "date": parse_date(date or "")})
    # de-duplicate, keep order
    seen, docs = set(), []
    for r in recs:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        docs.append(r)
    nxt = selector.css("div.page li.next a::attr(href), a.next::attr(href)").get()
    return unified(
        url, title="指导案例（列表）", document_links=docs,
        extra={
            "next_page": ex.absolutise(url, nxt) if nxt else None,
            "item_count": len(docs),
            "_evidence": {"title": evidence_for(selector, "div.page", snapshot_id)},
        },
    )


def court_detail(selector, url, snapshot_id):
    body = _text(selector, "div.txt_txt")
    meta = _text(selector, "div.detail_mes ul.message")
    return unified(
        url,
        title=_text(selector, "div.detail div.title"),
        published_at=parse_date(meta),
        body_text=body,
        attachments=find_attachments(selector, url),
        extra={
            "source_label": _text(selector, "div.detail_mes li"),
            "_evidence": {
                "title": evidence_for(selector, "div.detail div.title", snapshot_id),
                "published_at": evidence_for(selector, "div.detail_mes ul.message li:nth-child(2)", snapshot_id),
                "body_text": evidence_for(selector, "div.txt_txt", snapshot_id),
            },
        },
    )


# ---------------------------------------------------------------- B: ccgp
def ccgp_list(selector, url, snapshot_id):
    docs = find_document_links(selector, url)
    return unified(url, title=_text(selector, "title") or "中国政府采购网",
                   document_links=docs, extra={"item_count": len(docs)})


def ccgp_detail(selector, url, snapshot_id):
    body = _text(selector, "div.vF_detail_content")
    return unified(
        url,
        title=_text(selector, "div.vF_detail_header h2"),
        published_at=parse_date(_text(selector, "#pubTime") or _text(selector, "div.vF_detail_header p")),
        body_text=body,
        attachments=find_attachments(selector, url),
        extra={
            "source_label": _text(selector, "#sourceName"),
            "_evidence": {
                "title": evidence_for(selector, "div.vF_detail_header h2", snapshot_id),
                "published_at": evidence_for(selector, "#pubTime", snapshot_id),
                "body_text": evidence_for(selector, "div.vF_detail_content", snapshot_id),
            },
        },
    )


# ---------------------------------------------------------------- H: spp
def spp_list(selector, url, snapshot_id):
    docs = []
    seen = set()
    for a in selector.css('a[href*="/spp/jczdal/"]'):
        link = ex.absolutise(url, a.attrib.get("href", ""))
        text = ex._clean_text(a.xpath("string(.)").get() or "")
        if not link or not text or link.endswith("index.shtml") or link in seen:
            continue
        seen.add(link)
        docs.append({"url": link, "text": text, "date": None})
    return unified(url, title="指导性案例（列表）", document_links=docs,
                   extra={"item_count": len(docs), "next_page": None})


def spp_detail(selector, url, snapshot_id):
    meta = _text(selector, "div.detail_extend1")
    return unified(
        url,
        title=_text(selector, "div.detail_tit"),
        published_at=parse_date(meta),
        body_text=_text(selector, "#fontzoom"),
        attachments=find_attachments(selector, url),
        extra={
            "source_label": meta,
            "_evidence": {
                "title": evidence_for(selector, "div.detail_tit", snapshot_id),
                "published_at": evidence_for(selector, "div.detail_extend1", snapshot_id),
                "body_text": evidence_for(selector, "#fontzoom", snapshot_id),
            },
        },
    )


# ------------------------------------------------- D: Companies House
def companies_house_company(selector, url, snapshot_id):
    def dd(dt_label: str) -> str:
        for dl in selector.css("dl"):
            if (dl.css("dt::text").get() or "").strip() == dt_label:
                return ex._clean_text(dl.css("dd").xpath("string(.)").get() or "")
        return ""

    fields = {
        "company_name": _text(selector, "h1.heading-xlarge"),
        "company_number": _text(selector, "#company-number strong"),
        "registered_office_address": _text(selector, "#roa-address"),
        "company_status": _text(selector, "#company-status"),
        "company_type": _text(selector, "#company-type-value"),
        "incorporated_on": dd("Incorporated on"),
        "nature_of_business": " | ".join(ex._clean_text(n.xpath("string(.)").get())
                                         for n in selector.css('span[id^="sic"]')),
        "previous_names": " | ".join(ex._clean_text(n.xpath("string(.)").get())
                                     for n in selector.css('td[id^="previous-name-"]')),
        # Filing deadline, NOT the accounting period end (different dates).
        "accounts_next_due": ex._clean_text(selector.xpath(
            '//p[contains(., "Next accounts made up to")]/strong[last()]/text()'
        ).get() or ""),
    }
    body = " | ".join(f"{k}: {v}" for k, v in fields.items() if v)
    return unified(
        url,
        title=fields["company_name"],
        published_at=None,
        body_text=body,
        document_links=[],
        attachments=[],
        extra={
            "structured": fields,
            "_evidence": {
                "company_name": evidence_for(selector, "h1.heading-xlarge", snapshot_id),
                "company_number": evidence_for(selector, "#company-number", snapshot_id),
                "registered_office_address": evidence_for(selector, "#roa-address", snapshot_id),
                "company_status": evidence_for(selector, "#company-status", snapshot_id),
                "company_type": evidence_for(selector, "#company-type-value", snapshot_id),
                "incorporated_on": evidence_for(selector, "#company-creation-date", snapshot_id),
                "nature_of_business": evidence_for(selector, "#sic0", snapshot_id),
                "previous_names": evidence_for(selector, "#previousNameTable", snapshot_id),
                "accounts_next_due": {
                    "kind": "html_xpath", "snapshot_id": snapshot_id,
                    "xpath": '//p[contains(., "Next accounts made up to")]/strong[last()]',
                    "quote": fields["accounts_next_due"],
                },
            },
        },
    )


def companies_house_search(selector, url, snapshot_id):
    docs = []
    for a in selector.css('a[href*="/company/"]'):
        link = ex.absolutise(url, a.attrib.get("href", ""))
        text = ex._clean_text(a.xpath("string(.)").get() or "")
        if link and text and "/company/" in link:
            docs.append({"url": link, "text": text})
    return unified(url, title=_text(selector, "h1"), document_links=docs,
                   extra={"item_count": len(docs)})


# ------------------------------------------------------- E: stats.gov.cn
def stats_iframe_page(selector, url, snapshot_id):
    """list4.html itself is a shell; report the iframe targets explicitly."""
    iframes = []
    for f in selector.css("iframe"):
        src = f.attrib.get("src")
        u = ex.absolutise(url, src) if src else None
        if u:
            iframes.append({"url": u, "name": f.attrib.get("name", "")})
    return unified(url, title=_text(selector, "title"), body_text="",
                   document_links=find_document_links(selector, url),
                   extra={"iframes": iframes, "needs_iframe_follow": bool(iframes)})


def stats_generic_page(selector, url, snapshot_id):
    return unified(url, title=_text(selector, "title"),
                   body_text=_text(selector, "body"),
                   document_links=find_document_links(selector, url),
                   attachments=find_attachments(selector, url))


RECIPES = {
    "court_list": court_list,
    "court_detail": court_detail,
    "ccgp_list": ccgp_list,
    "ccgp_detail": ccgp_detail,
    "spp_list": spp_list,
    "spp_detail": spp_detail,
    "ch_company": companies_house_company,
    "ch_search": companies_house_search,
    "stats_iframe": stats_iframe_page,
    "stats_page": stats_generic_page,
}


def apply(recipe_name: str, html: str, url: str, snapshot_id: str):
    """Run a recipe against raw HTML text (offline-replay capable)."""
    from parsel import Selector
    sel = Selector(text=html, type="html")
    return RECIPES[recipe_name](sel, url, snapshot_id)