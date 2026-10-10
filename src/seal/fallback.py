"""Deterministic HTML projections used by reviewed Recipes and Locator checks.

The archived response is the only input. This module neither schedules requests
nor guesses business identities, executes JavaScript, or supplies literal data.
"""

import re
from datetime import date
from urllib.parse import urljoin, urlsplit

from parsel import Selector

from .core import SealError, public_url

_EXCLUDED = {"script", "style", "nav", "footer", "form", "svg", "noscript", "aside"}
_BLOCKS = {"p", "pre", "blockquote", "li", "table", "dt", "dd", *{f"h{i}" for i in range(1, 7)}}
_ATTACHMENTS = re.compile(r"\.(?:pdf|docx?|xlsx?|csv|txt|pptx?|zip|rtf|odt)$", re.I)
_DATE = re.compile(r"(?<!\d)(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})(?:日)?(?!\d)")
_DATE_PATH = (
    '//meta[@property="article:published_time" or @name="pubdate" or @name="publishdate" '
    'or @name="date" or @name="DC.date"]/@content | //time[@datetime]/@datetime | '
    '//time/text() | //*[@class="publish-date" or @class="published" '
    'or @class="pubdate" or @id="publish-date"]/text()'
)


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


def _path(node):
    return node.getroottree().getpath(node)


def _blocks(root):
    blocks = []

    def emit(kind, text, **extra):
        if text:
            blocks.append({"kind": kind, "text": text, **extra})

    def visit(node):
        tag = _tag(node)
        if not tag or tag in _EXCLUDED:
            return
        if tag == "table":
            rows = []
            for row in node.xpath(".//tr"):
                if next((p for p in row.iterancestors() if _tag(p) == "table"), None) is not node:
                    continue
                cells = []
                for cell in row.xpath("./td | ./th"):
                    spans = {}
                    for name in ("rowspan", "colspan"):
                        raw = cell.get(name, "1")
                        spans[name] = int(raw) if re.fullmatch(r"[1-9][0-9]{0,3}", raw) else 1
                    cells.append({"text": _text(cell), "header": _tag(cell) == "th", **spans})
                if cells:
                    rows.append(cells)
            if rows:
                blocks.append({"kind": "table", "rows": rows})
            return
        if tag in {f"h{i}" for i in range(1, 7)}:
            emit("heading", _text(node), level=int(tag[1]))
            return
        if tag == "li":
            parent = node.getparent()
            ordered = _tag(parent) == "ol"
            number = None
            if ordered:
                raw_start = parent.get("start", "1")
                number = int(raw_start) if re.fullmatch(r"-?[0-9]{1,6}", raw_start) else 1
                for sibling in parent:
                    if _tag(sibling) != "li":
                        continue
                    raw_value = sibling.get("value", "")
                    if re.fullmatch(r"-?[0-9]{1,6}", raw_value):
                        number = int(raw_value)
                    if sibling is node:
                        break
                    number += 1
            emit("list_item", _text(node, _EXCLUDED | {"ol", "ul"}), ordered=ordered, number=number)
            for child in node:
                if _tag(child) in {"ol", "ul"}:
                    visit(child)
            return
        if tag in {"p", "pre", "blockquote", "dt", "dd"}:
            emit("paragraph", _text(node))
            return
        # Plain div/span templates also retain text between their block children.
        pending = [node.text or ""]

        def flush():
            emit("paragraph", " ".join(" ".join(pending).split()))
            pending.clear()

        for child in node:
            child_tag = _tag(child)
            contains_block = any(
                _tag(descendant) in _BLOCKS for descendant in child.iterdescendants()
            )
            if child_tag in _BLOCKS or contains_block:
                flush()
                visit(child)
            elif child_tag not in _EXCLUDED:
                pending.append(_text(child))
            pending.append(child.tail or "")
        flush()

    visit(root)
    return blocks


def _body_text(blocks):
    lines = []
    for block in blocks:
        if block["kind"] == "table":
            lines.extend("\t".join(cell["text"] for cell in row) for row in block["rows"])
        else:
            text = block["text"]
            if block["kind"] == "list_item" and block["ordered"]:
                text = f"{block['number']}. {text}"
            lines.append(text)
    return "\n".join(lines)


def _attachment_links(root, url):
    links = []
    for anchor in root.xpath(".//a[@href]"):
        href = anchor.get("href")
        try:
            if not (_ATTACHMENTS.search(urlsplit(href).path) or "download" in anchor.attrib):
                continue
            target = public_url(urljoin(url, href))
        except (SealError, ValueError):
            continue
        link = {"url": target, "title": _text(anchor)}
        if link not in links:
            links.append(link)
    return links


def _published_date(selector):
    dates = set()
    for value in selector.xpath(_DATE_PATH).getall():
        match = _DATE.search(value)
        if match is None:
            continue
        try:
            dates.add(date(*map(int, match.groups())).isoformat())
        except ValueError:
            continue
    return dates.pop() if len(dates) == 1 else None


def html_locator_value(snapshot, body, locator):
    """Strict, source-backed projections, including an explicitly missing date."""
    kind = locator.get("kind")
    allowed = {
        "response_url": {"kind", "snapshot_id"},
        "html_date": {"kind", "snapshot_id"},
        "html_links": {"kind", "path", "snapshot_id"},
        "html_blocks": {"kind", "path", "selection", "snapshot_id"},
    }
    if kind not in allowed or set(locator) - allowed[kind]:
        raise SealError("invalid_html_locator")
    if kind == "response_url":
        return public_url(snapshot["url"])
    selector = Selector(body.decode(snapshot.get("encoding") or "utf-8", errors="strict"))
    if kind == "html_date":
        return _published_date(selector)
    nodes = selector.xpath(locator.get("path", ""))
    if len(nodes) != 1 or not hasattr(nodes[0].root, "getroottree"):
        raise SealError("ambiguous_or_missing_field")
    if kind == "html_links":
        return _attachment_links(nodes[0].root, snapshot["url"])
    blocks = _blocks(nodes[0].root)
    if locator.get("selection") == "structure":
        return blocks
    if locator.get("selection") == "text":
        return _body_text(blocks)
    raise SealError("invalid_html_locator")


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


def _directory_entry(node):
    return _tag(node) in {"div", "section", "article", "li"} and bool(
        node.xpath("./a[@href] | ./h1/a[@href] | ./h2/a[@href] | ./h3/a[@href]")
        and node.xpath("./p")
    )


def _directory_entry_count(parent):
    return sum(_directory_entry(child) for child in parent)


def _directory_content(root):
    if any(_directory_entry_count(parent) >= 3 for parent in root.iter()):
        return True
    # A summary remains a directory entry even when selected on its own.
    return any(
        _directory_entry(parent)
        and parent.getparent() is not None
        and _directory_entry_count(parent.getparent()) >= 3
        for parent in [root, *root.iterancestors()]
    )


def _content_node(response):
    candidates = response.css(
        'article,main,[role="main"],#content,#zoom,#fontzoom,.TRS_Editor,.article,.article-content,.content'
    )
    if not candidates:
        candidates = response.css("body")
    scored = []
    article_count = len(response.css("article"))
    for node in candidates:
        text = _text(node.root)
        link_chars = sum(len(_text(anchor)) for anchor in node.root.xpath(".//a"))
        ratio = link_chars / max(len(text), 1)
        semantic = _tag(node.root) == "article"
        # Search/news indexes can have long summaries with very little anchor
        # text. Their repeated linked entries remain discoveries, not documents.
        linked_items = len(node.root.xpath(".//li[.//a]"))
        paragraph_count = len(node.root.xpath(".//p"))
        if linked_items >= 3 and linked_items >= paragraph_count:
            continue
        # A directory may use div cards rather than list items. Link density
        # alone misses a short heading followed by a long column description.
        if _directory_content(node.root):
            continue
        if article_count > 1 and node.root.xpath(".//h1/a | .//h2/a | .//h3/a"):
            continue
        if len(text) < (30 if semantic else 80) or ratio > 0.5:
            continue
        score = len(text) - link_chars + (100 if semantic else 0)
        scored.append((score, node))
    if not scored:
        raise SealError("fallback_non_document")
    return max(scored, key=lambda value: value[0])[1]


def fallback_html_record(response, *, record_type="document", schema_version="record.v1"):
    """Extract conservative document content with deterministic archive evidence."""
    from .helpers import record_item

    _check_page(response)
    response.meta["seal_html_strategy"] = "fallback"
    node = _content_node(response)
    title_nodes = response.css("h1")
    if len(title_nodes) != 1:
        title_nodes = response.css("title")
    if len(title_nodes) != 1:
        raise SealError("ambiguous_or_missing_field")
    title_node = title_nodes[0]
    title = " ".join(title_node.xpath(".//text() | self::text()").getall()).strip()
    if not title:
        raise SealError("empty_document")
    path = _path(node.root)
    snapshot = {"url": response.url, "encoding": response.encoding}
    locators = {
        "title": {"kind": "xpath", "path": _path(title_node.root)},
        "body": {"kind": "html_blocks", "path": path, "selection": "text"},
        "blocks": {"kind": "html_blocks", "path": path, "selection": "structure"},
        "date": {"kind": "html_date"},
        "url": {"kind": "response_url"},
        "attachments": {"kind": "html_links", "path": path},
    }
    data = {"title": title}
    for field in ("body", "blocks", "date", "url", "attachments"):
        data[field] = html_locator_value(snapshot, response.body, locators[field])
    if not data["body"]:
        raise SealError("empty_document")
    url = public_url(response.url)
    return record_item(
        response,
        record_type=record_type,
        record_key=url,
        data=data,
        locators=locators,
        key_locator={"kind": "response_url"},
        detail_url=url,
        schema_version=schema_version,
    )


def html_strategy_record(
    response, *, specific_rules=(), record_type="document", schema_version="record.v1"
):
    """Select the first matching frozen Specific rule, then use Fallback.

    A matched but broken template is a parse failure. It cannot silently turn
    into an apparently successful generic document.
    """
    from .helpers import html_record

    _check_page(response)
    parsed = urlsplit(response.url)
    for rule in specific_rules:
        if not isinstance(rule, dict):
            raise SealError("invalid_specific_rule")
        if rule.get("hosts") and parsed.hostname not in rule["hosts"]:
            continue
        try:
            if rule.get("path_regex") and not re.search(rule["path_regex"], parsed.path):
                continue
            if rule.get("match_css") and not response.css(rule["match_css"]):
                continue
        except (ValueError, re.error):
            raise SealError("invalid_specific_rule") from None
        if not rule.get("title") or not rule.get("body"):
            raise SealError("invalid_specific_rule")
        response.meta["seal_html_strategy"] = "specific"
        item = html_record(
            response,
            title=rule["title"],
            body=rule["body"],
            date=rule.get("date"),
            record_type=rule.get("record_type", record_type),
            schema_version=rule.get("schema_version", schema_version),
        )
        body_node = response.css(rule["body"])[0]
        if _directory_content(body_node.root):
            raise SealError("fallback_non_document")
        path = _path(body_node.root)
        extra = {
            "blocks": {"kind": "html_blocks", "path": path, "selection": "structure"},
            "url": {"kind": "response_url"},
            "attachments": {"kind": "html_links", "path": path},
        }
        if "date" not in item["data"]:
            extra["date"] = {"kind": "html_date"}
        snapshot = {"url": response.url, "encoding": response.encoding}
        for field, locator in extra.items():
            item["locators"][field] = locator
            item["data"][field] = html_locator_value(snapshot, response.body, locator)
        return item
    return fallback_html_record(response, record_type=record_type, schema_version=schema_version)
