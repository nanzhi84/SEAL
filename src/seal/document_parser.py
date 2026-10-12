"""Pure, repeatable projections of archived resources into five content kinds.

Parsing does not fetch links or write objects. Embedded image references address
members of the original archived document; they do not claim separate downloads.
One article holds all prose, while tables and media retain their discovery order.
"""

import csv
import json
import re
import warnings
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zipfile import ZipFile

from lxml import etree
from PIL import Image
from pypdf import PdfReader
from scrapy.http import HtmlResponse

from .attachments import MAX_CELLS, MAX_DOCX_EXPANDED_BYTES, xls_structure
from .core import SealError, digest, public_url
from .document_office import _docx, _xlsx
from .html_safety import _check_page, _tag, _text
from .parser_identity import parser_version as parser_version

_FORMATS = {"html", "pdf", "docx", "xls", "xlsx", "csv", "txt", "json", "image"}
_OPTIONS = {"body_css", "title_css", "format", "csv_delimiter"}
_EXCLUDED = {"script", "style", "noscript", "svg"}
_BLOCKS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "li",
    "ul",
    "ol",
    "pre",
    "blockquote",
    "dt",
    "dd",
    *{f"h{i}" for i in range(1, 7)},
}
_ATTACHMENTS = re.compile(r"\.(?:pdf|docx?|xlsx?|csv|txt|json|pptx?|zip|rtf|odt)$", re.I)
_MIMES = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "text/csv": "csv",
    "application/csv": "csv",
    "text/plain": "txt",
    "application/json": "json",
    "text/json": "json",
}


def parse_options(options=None):
    """Return a plain frozen-options value, rejecting unrecognized adaptation."""
    if options is None:
        return {}
    if not isinstance(options, dict) or set(options) - _OPTIONS:
        raise SealError("invalid_document_parse_options")
    for name, value in options.items():
        if not isinstance(value, str) or not value:
            raise SealError("invalid_document_parse_options")
        if name != "csv_delimiter" and not value.strip():
            raise SealError("invalid_document_parse_options")
        if name == "format" and value not in _FORMATS:
            raise SealError("invalid_document_parse_options")
        if name == "csv_delimiter" and (len(value) != 1 or value in "\r\n\0"):
            raise SealError("invalid_document_parse_options")
    return options.copy()


def _header(snapshot, name):
    for key, value in snapshot.get("headers", {}).items():
        if key.lower() == name.lower():
            return value[0] if isinstance(value, list) and value else value
    return ""


def _decode(snapshot, body):
    content_type = _header(snapshot, "Content-Type")
    charset = re.search(r"charset\s*=\s*[\"']?([^\s;\"']+)", content_type, re.I)
    encoding = snapshot.get("encoding") or (charset[1] if charset else "utf-8-sig")
    try:
        return body.decode(encoding, errors="strict")
    except (UnicodeError, LookupError):
        raise SealError("document_decode_failed") from None


def _zip_budget(body, kind):
    try:
        package = ZipFile(BytesIO(body))
        entries = package.infolist()
        if len(entries) > 2000 or sum(entry.file_size for entry in entries) > (
            MAX_DOCX_EXPANDED_BYTES
        ):
            package.close()
            raise SealError(kind + "_expansion_budget_exceeded")
        return package
    except SealError:
        raise
    except Exception:
        raise SealError(kind + "_parse_failed") from None


def _format(snapshot, body, options):
    if "format" in options:
        return options["format"]
    mime = _header(snapshot, "Content-Type").split(";", 1)[0].strip().lower()
    if mime.startswith("image/"):
        return "image"
    if mime in _MIMES:
        return _MIMES[mime]
    if mime.endswith("+json"):
        return "json"
    suffix = Path(urlsplit(snapshot["url"]).path).suffix.lower().lstrip(".")
    if suffix in _FORMATS:
        return suffix
    if suffix in {"htm", "xhtml"}:
        return "html"
    if suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif", "svg", "ico"}:
        return "image"
    if body.startswith(b"%PDF-"):
        return "pdf"
    if body.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls"
    if body.startswith(b"PK\x03\x04"):
        with _zip_budget(body, "document") as package:
            paths = set(package.namelist())
            if "word/document.xml" in paths:
                return "docx"
            if "xl/workbook.xml" in paths:
                return "xlsx"
    if snapshot.get("response_type") == "HtmlResponse":
        return "html"
    if snapshot.get("response_type") == "JsonResponse":
        return "json"
    raise SealError("unsupported_content_type")


class _Content:
    def __init__(self):
        self.children = []
        self.article = None
        self.lines = []
        self.cells = 0

    def text(self, value):
        value = value.strip()
        if not value:
            return
        if self.article is None:
            self.article = {"type": "article", "text": "", "format": "text"}
            self.children.append(self.article)
        self.lines.append(value)

    def table(self, name, rows):
        width = max(map(len, rows), default=0)
        self.cells += len(rows) * width
        if self.cells > MAX_CELLS:
            raise SealError("document_cell_budget_exceeded")
        self.children.append(
            {"type": "table", "name": name, "rows": [r + [None] * (width - len(r)) for r in rows]}
        )

    def finish(self):
        if self.article is not None:
            self.article["text"] = "\n".join(self.lines)
        return self.children


def _target(url, href):
    try:
        return public_url(urljoin(url, href))
    except (SealError, ValueError):
        return None


def _html_table(node):
    source_rows = [
        row
        for row in node.xpath(".//tr")
        if next((p for p in row.iterancestors() if _tag(p) == "table"), None) is node
    ]
    grid = {}
    height, width = len(source_rows), 0
    for r, row in enumerate(source_rows):
        c = 0
        for cell in row.xpath("./td | ./th"):
            while (r, c) in grid:
                c += 1
            spans = []
            for key in ("rowspan", "colspan"):
                raw = cell.get(key, "1")
                spans.append(int(raw) if re.fullmatch(r"[1-9][0-9]{0,5}", raw) else 1)
            rowspan, colspan = spans
            height, width = max(height, r + rowspan), max(width, c + colspan)
            if height * width > MAX_CELLS:
                raise SealError("document_cell_budget_exceeded")
            for rr in range(r, r + rowspan):
                for cc in range(c, c + colspan):
                    if (rr, cc) in grid:
                        raise SealError("html_table_overlap")
                    grid[rr, cc] = _text(cell) if (rr, cc) == (r, c) else None
            c += colspan
    return [[grid.get((r, c)) for c in range(width)] for r in range(height)]


def _html(snapshot, body, options):
    response = HtmlResponse(
        snapshot["url"], body=_decode(snapshot, body).encode(), encoding="utf-8"
    )
    response = response.replace(status=snapshot.get("status", 200))
    _check_page(response)
    try:
        if "body_css" in options:
            selected = response.css(options["body_css"])
            if len(selected) != 1:
                raise SealError("ambiguous_or_missing_field")
            root = selected[0].root
        else:
            selected = response.css("main,article,[role=main]")
            selected_ids = {id(node.root) for node in selected}
            outer = [
                node
                for node in selected
                if not any(id(parent) in selected_ids for parent in node.root.iterancestors())
            ]
            root = outer[0].root if len(outer) == 1 else response.css("body")[0].root
        title = None
        if "title_css" in options:
            selected_title = response.css(options["title_css"])
            if len(selected_title) != 1:
                raise SealError("ambiguous_or_missing_field")
            title = selected_title[0].root
    except SealError:
        raise
    except Exception:
        raise SealError("invalid_document_parse_options") from None
    content, pending, table_count = _Content(), [], 0
    if title is not None and title is not root and root not in title.iterancestors():
        content.text(_text(title))

    def flush():
        content.text(" ".join(" ".join(pending).split()))
        pending.clear()

    def visit(node):
        nonlocal table_count
        tag = _tag(node)
        if not tag or tag in _EXCLUDED:
            return
        if tag in _BLOCKS:
            flush()
        if tag == "table":
            flush()
            table_count += 1
            captions = node.xpath("./caption")
            content.table(
                _text(captions[0]) if captions else f"Table {table_count}", _html_table(node)
            )
            return
        if tag == "img":
            flush()
            url = _target(snapshot["url"], node.get("src", "")) if node.get("src") else None
            if url:
                image = {"type": "image", "url": url}
                caption = node.get("alt") or node.get("title")
                if caption:
                    image["caption"] = caption
                content.children.append(image)
            return
        if tag == "br":
            flush()
            return
        if tag == "a" and node.get("href"):
            href = node.get("href")
            url = _target(snapshot["url"], href)
            if (
                url
                and _ATTACHMENTS.search(urlsplit(url).path)
                or "download" in node.attrib
                or "attachment" in node.get("class", "").split()
                or node.get("rel") == "attachment"
            ):
                if url:
                    flush()
                    ref = {"type": "document_ref", "url": url, "relation": "attachment"}
                    name = _text(node)
                    if name:
                        ref["name"] = name
                    if ref not in content.children:
                        content.children.append(ref)
        if node.text:
            pending.append(node.text)
        for child in node:
            visit(child)
            if child.tail:
                pending.append(child.tail)
        if tag in _BLOCKS:
            flush()

    try:
        visit(root)
        flush()
        return content.finish()
    except SealError:
        raise
    except Exception:
        raise SealError("html_parse_failed") from None


def _image_reference(snapshot, member=None, caption=None):
    node = {"type": "image", "url": public_url(snapshot["url"]), "body_hash": snapshot["body_hash"]}
    if member:
        node["archive_member"] = member
    if caption:
        node["caption"] = caption
    return node


def _pdf(snapshot, body):
    try:
        reader = PdfReader(BytesIO(body), strict=True)
        if reader.is_encrypted:
            raise SealError("pdf_encrypted")
        content = _Content()
        for number, page in enumerate(reader.pages, 1):
            content.text(page.extract_text() or "")
            resources = page.get("/Resources")
            if not resources:
                continue
            resources = resources.get_object()

            def images(resource, path, ancestors):
                if len(ancestors) > 16:
                    raise SealError("pdf_image_structure_too_deep")
                xobjects = resource.get("/XObject")
                if not xobjects:
                    return
                for name, indirect in sorted(xobjects.get_object().items()):
                    obj = indirect.get_object()
                    if id(obj) in ancestors:
                        continue
                    member = path + "/XObject/" + str(name).lstrip("/")
                    if obj.get("/Subtype") == "/Image":
                        content.children.append(_image_reference(snapshot, member))
                    elif obj.get("/Subtype") == "/Form" and obj.get("/Resources"):
                        images(obj["/Resources"].get_object(), member, ancestors | {id(obj)})

            images(resources, f"pdf/pages/{number}", set())
        if content.article is None:
            raise SealError("pdf_text_layer_required")
        return content.finish()
    except SealError:
        raise
    except Exception:
        raise SealError("pdf_parse_failed") from None


def _image(snapshot, body):
    try:
        mime = _header(snapshot, "Content-Type").split(";", 1)[0].lower()
        if mime == "image/svg+xml" or urlsplit(snapshot["url"]).path.lower().endswith(".svg"):
            root = etree.fromstring(body, etree.XMLParser(resolve_entities=False, no_network=True))
            if etree.QName(root).localname != "svg":
                raise SealError("image_parse_failed")
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(body)) as image:
                    image.verify()
        return [_image_reference(snapshot)]
    except SealError:
        raise
    except Exception:
        raise SealError("image_parse_failed") from None


def parse_document(snapshot, body, *, options=None):
    """Parse one archived response, raising stable diagnostics for failed inputs.

    Formats are inferred from media type, URL, signatures, and response type.
    Explicit options are part of the immutable processing contract. XLSX formulas
    remain source formulas; this function never calculates them or performs OCR.
    """
    options = parse_options(options)
    if not isinstance(snapshot, dict) or not isinstance(body, bytes):
        raise SealError("invalid_document_input")
    if not isinstance(snapshot.get("url"), str):
        raise SealError("invalid_document_input")
    if len(body) > MAX_DOCX_EXPANDED_BYTES:
        raise SealError("document_input_budget_exceeded")
    if snapshot.get("status", 200) != 200:
        raise SealError("document_http_status_rejected")
    public_url(snapshot["url"])
    if snapshot.get("body_hash") != digest(body):
        raise SealError("document_body_hash_mismatch")
    kind = _format(snapshot, body, options)
    if kind == "html":
        children = _html(snapshot, body, options)
    elif kind == "pdf":
        children = _pdf(snapshot, body)
    elif kind == "docx":
        children = _docx(snapshot, body)
    elif kind == "xls":
        content = _Content()
        for sheet in xls_structure(body):
            content.table(sheet["name"], sheet["rows"])
        children = content.finish()
    elif kind == "xlsx":
        children = _xlsx(body)
    elif kind == "csv":
        try:
            content = _Content()
            rows, width = [], 0
            for row in csv.reader(
                StringIO(_decode(snapshot, body), newline=""),
                delimiter=options.get("csv_delimiter", ","),
                strict=True,
            ):
                rows.append(row)
                width = max(width, len(row))
                if len(rows) * width > MAX_CELLS:
                    raise SealError("document_cell_budget_exceeded")
            content.table("CSV", rows)
            children = content.finish()
        except SealError:
            raise
        except (csv.Error, ValueError):
            raise SealError("csv_parse_failed") from None
    elif kind == "txt":
        text = _decode(snapshot, body).strip()
        if not text:
            raise SealError("empty_document")
        children = [{"type": "article", "text": text, "format": "text"}]
    elif kind == "json":
        try:

            def reject_constant(value):
                raise ValueError("nonfinite JSON number")

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate JSON object key")
                    result[key] = value
                return result

            value = json.loads(
                _decode(snapshot, body),
                parse_constant=reject_constant,
                object_pairs_hook=unique_object,
            )
            digest(value)
            children = [{"type": "json", "value": value}]
        except SealError:
            raise
        except (ValueError, TypeError, RecursionError):
            raise SealError("json_parse_failed") from None
    else:
        children = _image(snapshot, body)
    if not children or all(
        node["type"] == "article" and not node["text"].strip() for node in children
    ):
        raise SealError("empty_document")
    return {"format": kind, "children": children}
