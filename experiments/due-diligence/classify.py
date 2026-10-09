"""Conservative observable page classification; reachability is not business coverage."""

import re
from io import BytesIO

from inventory import clean_url
from parsel import Selector
from pypdf import PdfReader

CHALLENGE = re.compile(
    r"访问过于频繁|安全验证|人机验证|滑动验证|拖动滑块|访问被拒绝|拒绝访问|请求被拦截|网站防火墙|access denied|just a moment|verify you are human|checking your browser|request rejected|captcha challenge",
    re.I,
)
ERROR_PAGE = re.compile(
    r"404.{0,15}(?:not found|错误)|页面(?:不存在|未找到|无法访问)|您访问的页面|网站维护中|系统维护中|网站已关闭|not found|bad gateway|service unavailable",
    re.I,
)


def is_robots_document(body):
    text = body.decode("utf-8", "replace")
    return not re.search(
        r"<(?:!doctype|html|head|body|script)\b", text, re.I
    ) and not CHALLENGE.search(text)


def extract(response):
    mime = response.headers.get("Content-Type", b"").decode("latin1").split(";")[0].lower()
    base = {
        "mime": mime,
        "title": "",
        "text_chars": 0,
        "links": [],
        "inputs": [],
        "scripts": [],
        "iframes": [],
        "business_verified": False,
    }
    if response.status != 200:
        return dict(base, outcome="HTTP_ERROR", reason=f"HTTP_{response.status}")
    if re.search(rb"""["']_waf_[a-f0-9]+["']""", response.body, re.I):
        return dict(base, outcome="ACCESS_RESTRICTED", reason="WAF_SCRIPT_CHALLENGE")
    if mime == "application/pdf" or response.body.startswith(b"%PDF"):
        try:
            reader = PdfReader(BytesIO(response.body), strict=False)
            texts = [(p.extract_text() or "").strip() for p in reader.pages]
            text = "\n".join(texts)
            return dict(
                base,
                title=(text.splitlines() or [""])[0][:300],
                text_chars=len(text),
                pages=len(texts),
                excerpt=text[:500],
                outcome="ACCESSIBLE_PDF" if all(texts) else "PDF_NO_TEXT",
                reason="PDF_TEXT_LAYER_EXTRACTED" if all(texts) else "OCR_REQUIRED",
                parser="pypdf.text-pages.v1",
            )
        except Exception as exc:
            return dict(base, outcome="PARSE_ERROR", reason="PDF_" + type(exc).__name__)
    if "json" in mime:
        import json

        try:
            data = json.loads(response.body)
            return dict(
                base,
                outcome="ACCESSIBLE_JSON",
                reason="PUBLIC_JSON_RESPONSE",
                text_chars=len(str(data)),
                json_shape=type(data).__name__,
                parser="json.v1",
            )
        except ValueError:
            return dict(base, outcome="PARSE_ERROR", reason="INVALID_JSON")
    try:
        text = response.text
    except AttributeError:
        return dict(base, outcome="UNSUPPORTED", reason="NON_TEXT_CONTENT")
    sel = Selector(text)
    title = " ".join(sel.css("title::text").getall()).strip()
    title = title or " ".join(sel.css("h1::text").getall()).strip()
    visible = " ".join(
        t.strip()
        for t in sel.xpath(
            "//body//text()[not(ancestor::script or ancestor::style or ancestor::noscript)]"
        ).getall()
        if t.strip()
    )
    if not visible:
        visible = " ".join(
            t.strip()
            for t in sel.xpath(
                "//text()[not(ancestor::script or ancestor::style or ancestor::head or ancestor::noscript)]"
            ).getall()
            if t.strip()
        )
    links = []
    seen = set()
    for anchor in sel.css("a[href]"):
        href = response.urljoin(anchor.attrib["href"])
        if not href.startswith(("http://", "https://")):
            continue
        name = " ".join(anchor.xpath(".//text()").getall()).strip()
        href = clean_url(href)
        if name and (name, href) not in seen:
            links.append({"text": name[:160], "url": href})
            seen.add((name, href))
    inputs = [
        {
            "type": node.attrib.get("type", "text"),
            "name": node.attrib.get("name", ""),
            "placeholder": node.attrib.get("placeholder", "")[:100],
        }
        for node in sel.css("input")
        if node.attrib.get("type", "text").lower()
        not in ("hidden", "submit", "button", "checkbox", "radio")
    ]
    scripts = [
        clean_url(response.urljoin(u))
        for u in sel.css("script[src]::attr(src)").getall()
        if response.urljoin(u).startswith(("http://", "https://"))
    ]
    frames = [
        clean_url(response.urljoin(u))
        for u in sel.css("iframe[src]::attr(src)").getall()
        if response.urljoin(u).startswith(("http://", "https://"))
    ]
    base.update(
        title=title[:300],
        text_chars=len(visible),
        links=links[:100],
        link_count=len(links),
        inputs=inputs[:12],
        scripts=scripts[:25],
        iframes=frames[:8],
        excerpt=visible[:450],
        parser="html.title-links.v1",
        title_xpath="//title/text()" if sel.css("title") else "//h1//text()",
    )
    if CHALLENGE.search(title + " " + visible[:1200]):
        return dict(base, outcome="ACCESS_RESTRICTED", reason="CHALLENGE_OR_ACCESS_DENIED")
    if len(visible) < 3000 and ERROR_PAGE.search(title + " " + visible):
        return dict(base, outcome="SOFT_ERROR", reason="HTTP_200_ERROR_PAGE")
    if any(i["type"] == "password" for i in inputs) or re.search(
        r"^.{0,10}(?:登录|登錄|login|sign in).{0,20}$", title, re.I
    ):
        return dict(base, outcome="SKIP_INPUT", reason="LOGIN_REQUIRED")
    if len(visible) < 80 and (
        scripts or frames or sel.css("#app,#root,[ng-app]") or "javascript" in text.lower()
    ):
        return dict(base, outcome="JS_SHELL", reason="JS_OR_IFRAME_REQUIRED")
    if len(visible) < 40:
        return dict(base, outcome="EMPTY", reason="INSUFFICIENT_VISIBLE_CONTENT")
    if inputs and len(visible) < 2500 and len(links) < 15:
        return dict(base, outcome="SKIP_INPUT", reason="QUERY_PARAMETERS_REQUIRED")
    return dict(base, outcome="ACCESSIBLE_HTML", reason="PUBLIC_ENTRY_TEXT_EXTRACTED")
