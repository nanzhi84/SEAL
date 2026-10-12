"""Shared HTML text and access-control/error-page recognition."""

import re

from .core import SealError

_EXCLUDED = {"script", "style", "nav", "footer", "form", "svg", "noscript", "aside"}


def _tag(node):
    return node.tag.lower() if isinstance(node.tag, str) else ""


def _text(node, excluded=_EXCLUDED):
    """Keep visible text in source order without script or navigation payloads."""
    chunks = []

    def visit(current):
        if not _tag(current) or _tag(current) in excluded:
            return
        if current.text:
            chunks.append(" ".join(current.text.split()))
        for child in current:
            if _tag(child) == "br":
                chunks.append("\n")
            else:
                visit(child)
            if child.tail:
                chunks.append(" ".join(child.tail.split()))

    visit(node)
    return re.sub(r" *\n *", "\n", re.sub(r"[^\S\n]+", " ", " ".join(chunks))).strip()


def _check_page(response):
    if response.status != 200:
        raise SealError("fallback_error_page")
    if not hasattr(response, "css"):
        raise SealError("unsupported_content_type")
    headings = [
        " ".join(node.xpath(".//text()").getall()).strip() for node in response.css("title,h1")
    ]
    if response.css(
        'input[type="password"], #challenge-form, .g-recaptcha, #cf-challenge-running, '
        ".paywall,[data-paywall]"
    ):
        raise SealError("access_control_detected")
    visible = _text(response.selector.root)
    authentication_prompt = re.search(
        r"(?:请|必须|需要|须)(?:先)?(?:登录|登入|认证|验证身份|完成认证).{0,40}(?:查看|访问|继续|内容|阅读)|"
        r"(?:sign|log)[ -]?in\s+to\s+(?:continue|view|access|read)|"
        r"authentication (?:is )?required|you (?:must|need to) (?:sign|log)[ -]?in",
        visible,
        re.I,
    )
    authentication_link = any(
        re.search(
            r"(?:^|[/?._-])(?:sso|login|signin|authenticate)(?:[/?._-]|$)",
            anchor.get("href", ""),
            re.I,
        )
        or re.search(r"登录|登入|身份认证|sign[ -]?in|log[ -]?in", _text(anchor), re.I)
        for anchor in response.selector.root.xpath(".//a[@href]")
    )
    if authentication_prompt and (authentication_link or len(visible) < 1500):
        raise SealError("access_control_detected")
    if any(
        re.search(
            r"^(?:sign[ -]?in|log[ -]?in|登录|用户登录|just a moment|verify (?:you are|your)|"
            r"请.*验证|captcha|verification required|security check|subscription required)",
            text,
            re.I,
        )
        for text in headings
    ):
        raise SealError("access_control_detected")
    if any(
        re.search(
            r"^(?:[45][0-9]{2}(?:\D|$)|not found|access denied|forbidden|server error|"
            r"error(?:\b|$)|something went wrong|temporarily unavailable|页面不存在|访问被拒绝|系统错误)",
            text,
            re.I,
        )
        for text in headings
    ):
        raise SealError("fallback_error_page")
