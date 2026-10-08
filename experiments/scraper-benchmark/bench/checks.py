"""Independent quality checks that do NOT reuse the extraction recipes.

They read the archived raw bytes directly, so they can be compared against any
framework's output without circularity (the extraction is never used to grade
itself).
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

import lxml.html

_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)


def visible_text(html: str) -> str:
    """Visible text of the raw HTML, computed independently of any recipe."""
    if not html:
        return ""
    stripped = _SCRIPT_STYLE.sub(" ", html)
    try:
        doc = lxml.html.fromstring(stripped)
        txt = doc.text_content()
    except Exception:  # noqa: BLE001
        txt = re.sub(r"<[^>]+>", " ", stripped)
    return re.sub(r"[\s\u00a0\u3000]+", " ", txt).strip()


def independent_detail_urls(html: str, base_url: str, pattern: str) -> list[str]:
    """Regex enumeration of detail links -- independent of the recipe selectors."""
    out, seen = [], set()
    for href in re.findall(r'href="([^"]+)"', html or ""):
        if re.search(pattern, href):
            url = urljoin(base_url, href)
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


_NAV_MARKERS = [
    "首页", "上一页", "下一页", "尾页", "网站地图", "联系我们", "关于我们",
    "版权所有", "无障碍浏览", "登录邮箱系统", "打印本页", "字号", "分享到",
    "主办单位", "技术支持", "友情链接", "关注我们", "京ICP备",
]


def nav_leak(body_text: str) -> dict:
    """How much navigation/footer/boilerplate text leaked into the body."""
    if not body_text:
        return {"marker_hits": 0, "hits": [], "leak_ratio": None}
    hits = [m for m in _NAV_MARKERS if m in body_text]
    head = body_text[:2000]
    return {
        "marker_hits": len(hits),
        "hits": hits[:12],
        "leak_ratio": round(len(hits) / len(_NAV_MARKERS), 4),
        "body_chars": len(body_text),
        "head_preview": head[:200],
    }


def traceability(body_text: str, raw_html: str) -> dict:
    """Fraction of extracted body lines (len>=8) that literally occur in the raw
    visible text. Detects fabricated / reordered / summarised content."""
    if not body_text:
        return {"checked_lines": 0, "matched": 0, "containment": None}
    vis = visible_text(raw_html)
    lines = [ln.strip() for ln in body_text.split("\n") if len(ln.strip()) >= 8]
    if not lines:
        return {"checked_lines": 0, "matched": 0, "containment": None}
    matched = sum(1 for ln in lines if ln in vis)
    return {"checked_lines": len(lines), "matched": matched,
            "containment": round(matched / len(lines), 4)}


def completeness(body_text: str, raw_html: str) -> dict:
    """Rough recall proxy: extracted chars vs. the raw page's visible chars."""
    vis = visible_text(raw_html)
    bl = len(re.sub(r"\s", "", body_text or ""))
    vl = len(re.sub(r"\s", "", vis))
    return {
        "body_chars": bl,
        "raw_visible_chars": vl,
        "body_vs_raw_ratio": round(bl / vl, 4) if vl else None,
    }


def field_in_raw(value: str, raw_html: str) -> bool | None:
    if value is None:
        return None
    v = re.sub(r"\s+", "", str(value))
    if not v:
        return None
    h = re.sub(r"\s+", "", raw_html or "")
    return v in h