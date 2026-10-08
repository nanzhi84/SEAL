"""Framework-neutral extraction helpers.

These are the parts a SEAL maintainer would write once and reuse across
frameworks: generic main-content extraction, link/attachment discovery,
evidence capture, and content canonicalisation for revision detection.

`extract_generic` uses trafilatura (mature third-party library) so the Scrapy
side is not artificially limited to hand-written XPath. `*_generic` output is
directly comparable to Firecrawl's markdown/metadata extraction.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, urldefrag

ATTACHMENT_EXT = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".txt", ".csv", ".rtf", ".wps",
)
CONTENT_HINT = re.compile(
    r"(xiangqing|jczdal|article|content|detail|news|zcdt|zcfg|gonggao|notice|"
    r"t\d{8}_\d+|/\d{4,}\.html|\.shtml|\.htm$)",
    re.I,
)
NAV_NOISE = re.compile(
    r"^(首页|上一页|下一页|尾页|更多|返回|登录|注册|English|中文版|网站地图|"
    r"联系我们|关于我们|无障碍浏览|打印|分享|关闭|顶部)$"
)


def _clean_text(node_text: str) -> str:
    return re.sub(r"[ \t\u00a0\u3000]+", " ", node_text or "").strip()


def absolutise(base: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    url, _frag = urldefrag(urljoin(base, href))
    return url or None


def same_site(base: str, url: str) -> bool:
    a, b = urlparse(base), urlparse(url)
    return a.netloc == b.netloc


def find_attachments(selector, base_url: str) -> list[dict]:
    out, seen = [], set()
    for a in selector.css("a"):
        href = a.attrib.get("href", "")
        url = absolutise(base_url, href)
        if not url:
            continue
        if urlparse(url).path.lower().endswith(ATTACHMENT_EXT) and url not in seen:
            seen.add(url)
            out.append({"url": url, "text": _clean_text(a.xpath("string(.)").get() or "")})
    return out


def find_document_links(selector, base_url: str, *, require_hint: bool = True) -> list[dict]:
    """Same-site links that plausibly lead to documents (list -> detail)."""
    out, seen = [], set()
    for a in selector.css("a"):
        href = a.attrib.get("href", "")
        url = absolutise(base_url, href)
        if not url or not same_site(base_url, url):
            continue
        text = _clean_text(a.xpath("string(.)").get() or "")
        if not text or NAV_NOISE.match(text):
            continue
        path = urlparse(url).path
        if require_hint and not CONTENT_HINT.search(path):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "text": text})
    return out


def xpath_of(node) -> str:
    try:
        return node.root.getroottree().getpath(node.root)
    except Exception:  # noqa: BLE001
        return ""


def evidence_for(selector, css: str, snapshot_id: str) -> dict | None:
    """Field evidence: XPath + verbatim quote from the raw response."""
    nodes = selector.css(css)
    if not nodes:
        return None
    node = nodes[0]
    return {
        "kind": "html_xpath",
        "snapshot_id": snapshot_id,
        "css": css,
        "xpath": xpath_of(node),
        "quote": (node.xpath("string(.)").get() or "")[:600],
    }


_DATE_PATTERNS = [
    (re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})"), "{:04d}-{:02d}-{:02d}"),
]


def parse_date(text: str) -> str | None:
    if not text:
        return None
    for pat, fmt in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return fmt.format(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:  # noqa: BLE001
                continue
    return None


def extract_generic(html: str, url: str) -> dict:
    """Generic (non-site-specific) extraction via trafilatura.

    Mirrors what Firecrawl's built-in main-content extraction offers, so the two
    frameworks can be compared on the same footing.
    """
    import logging

    import trafilatura
    from trafilatura import extract

    logging.getLogger("trafilatura").setLevel(logging.ERROR)

    result = {"body_text": "", "title": None, "published_at": None, "error": None}
    try:
        txt = extract(html, url=url, include_comments=False, include_tables=True,
                      favor_precision=True, output_format="txt")
        result["body_text"] = _clean_text(txt or "")
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
        if meta is not None:
            result["title"] = getattr(meta, "title", None)
            result["published_at"] = getattr(meta, "date", None)
    except Exception:  # noqa: BLE001
        pass
    return result


def unified(source_url: str, *, title=None, published_at=None, body_text="",
            document_links=None, attachments=None, extra=None) -> dict:
    """The SEAL V1 canonical JSON shape used across all experiments."""
    doc = {
        "source_url": source_url,
        "title": title or "",
        "published_at": published_at,
        "body_text": body_text or "",
        "document_links": document_links or [],
        "attachments": attachments or [],
    }
    if extra:
        doc.update(extra)
    return doc