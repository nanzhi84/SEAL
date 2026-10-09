"""Small evidence-preserving helpers for reviewed, ordinary Python recipes.

These functions only parse responses and emit Scrapy requests/items. Scheduling,
request fingerprints, scope checks, archival and retries remain Runtime/Scrapy work.
"""

from io import BytesIO
from urllib.parse import urlsplit

import scrapy
from pypdf import PdfReader
from scrapy.http import TextResponse

from .attachments import attachment_projection, docx_coverage, parse_attachment
from .core import SealError, public_url
from .record_validation import json_pointer


def input_reference(response):
    return {
        "snapshot_id": response.meta["seal_snapshot_id"],
        "observation_id": response.meta["seal_observation_id"],
    }


def diagnostic(response, code):
    """Attribute parse failures to archived inputs, including per-row failures."""
    return {"type": "diagnostic", "code": code, **input_reference(response)}


def frozen_request(response):
    """Keep the actual frozen GET/query, never invent a detail URL for a row."""
    if response.request.method != "GET" or response.request.body:
        raise SealError("record_parent_request_rejected")
    return {
        "url": public_url(response.meta.get("seal_logical_url", response.request.url)),
        "role": response.meta.get("seal_role", "list"),
        "method": "GET",
    }


def follow(response, url, role, callback, errback=None, *, cb_kwargs=None, rejection=None):
    """Create a new native Request with explicit discovery parent evidence.

    Do not copy a parent's request/snapshot/observation identifiers: the new HTTP
    exchange receives its own identifiers in the Runtime archival lifecycle.
    A rejected iframe is still emitted so Discovery records it before scheduling.
    """
    meta = {
        "seal_role": role,
        "seal_parent_url": response.url,
        "seal_parent_snapshot_id": response.meta["seal_snapshot_id"],
        "seal_parent_observation_id": response.meta["seal_observation_id"],
    }
    if rejection:
        meta["seal_helper_rejection"] = rejection
    return scrapy.Request(
        response.urljoin(url), callback=callback, errback=errback, meta=meta, cb_kwargs=cb_kwargs
    )


def link_requests(response, selector, role, callback, errback=None):
    """Emit every discovery, including duplicate links; Scrapy owns deduplication."""
    for node in response.css(selector):
        href = node.attrib.get("href")
        if href:
            yield follow(response, href, role, callback, errback)
        else:
            yield diagnostic(response, "missing_document_href")


def static_resources(
    response,
    *,
    iframe_callback,
    attachment_callback,
    errback=None,
    iframe_selector="iframe[src]",
    attachment_selector="a.attachment[href]",
):
    """Follow only explicit business resources, with same-host static iframes.

    Attachment host/path permission is checked against the immutable Source by
    RequestGuard, including explicit cross-host scopes. Presentation assets are
    never implicitly selected. Each attachment retains its parent as an input.
    """
    for node in response.css(iframe_selector) if iframe_selector else ():
        url = response.urljoin(node.attrib["src"])
        same_host = urlsplit(url).hostname == urlsplit(response.url).hostname
        yield follow(
            response,
            url,
            "iframe",
            iframe_callback,
            errback,
            rejection=None if same_host else "iframe_out_of_scope",
        )
    for node in response.css(attachment_selector) if attachment_selector else ():
        href = node.attrib.get("href")
        if not href:
            yield diagnostic(response, "missing_document_href")
            continue
        parent = input_reference(response)
        yield follow(
            response,
            href,
            "attachment",
            attachment_callback,
            errback,
            cb_kwargs={"parent": parent},
        )


def node_text(node):
    return " ".join(node.xpath(".//text() | self::text()").getall()).strip()


def xpath_locator(node):
    if not hasattr(node.root, "getroottree"):
        raise SealError("element_selector_required")
    return {"kind": "xpath", "path": node.root.getroottree().getpath(node.root)}


def selected_field(response, selector):
    nodes = response.css(selector)
    if len(nodes) != 1:
        raise SealError("ambiguous_or_missing_field")
    return node_text(nodes[0]), xpath_locator(nodes[0])


def record_item(
    response,
    *,
    record_type,
    record_key,
    data,
    locators,
    key_locator,
    detail_url=None,
    schema_version="record.v1",
    supplementary_inputs=None,
):
    return {
        "type": "record",
        "record_type": record_type,
        "record_key": record_key,
        "schema_version": schema_version,
        "data": data,
        "detail_url": detail_url,
        **input_reference(response),
        "locators": locators,
        "key_locator": key_locator,
        "frozen_parent_request": frozen_request(response)
        if key_locator.get("kind") != "response_url" or detail_url is None
        else None,
        "supplementary_inputs": supplementary_inputs or [],
    }


def html_record(
    response,
    *,
    title="h1",
    body="article",
    date="time",
    record_type="document",
    schema_version="record.v1",
):
    """A single detail document with URL identity and absolute XPath evidence."""
    data, locators = {}, {}
    for field, selector in (("title", title), ("body", body), ("date", date)):
        if selector is None or field == "date" and not response.css(selector):
            continue
        data[field], locators[field] = selected_field(response, selector)
    if not data.get("title") or not data.get("body"):
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


def table_records(
    response,
    *,
    rows,
    fields,
    key_field,
    record_type="table_row",
    schema_version="record.v1",
):
    """Project reviewed table cells, proving a stable business key from a cell.

    Row position is used only in the absolute XPath evidence, never as identity.
    A malformed row emits a diagnostic while retaining all other valid rows.
    """
    if key_field not in fields:
        raise SealError("record_key_field_missing")
    for row in response.css(rows):
        try:
            data, locators = {}, {}
            for name, selector in fields.items():
                nodes = row.css(selector)
                if len(nodes) != 1:
                    raise SealError("ambiguous_or_missing_field")
                data[name], locators[name] = node_text(nodes[0]), xpath_locator(nodes[0])
            if not data[key_field]:
                raise SealError("record_key_missing")
            yield record_item(
                response,
                record_type=record_type,
                record_key=data[key_field],
                data=data,
                locators=locators,
                key_locator=locators[key_field].copy(),
                schema_version=schema_version,
            )
        except (KeyError, TypeError, ValueError, SealError) as exc:
            yield diagnostic(
                response, exc.code if isinstance(exc, SealError) else "table_row_parse_failed"
            )


def pointer_part(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def json_records(
    response,
    *,
    pointer="/data/data/dataList",
    key_field="id",
    record_type="entity",
    schema_version="record.v1",
    detail_url_field=None,
):
    """Preserve native JSON types and JSON Pointer evidence for each API object.

    Only a stable string/integer API key is accepted. Array offsets identify the
    evidence location, not business identity. Envelope fields stay out of data.
    """
    rows = json_pointer(response.body, pointer)
    if not isinstance(rows, list):
        raise SealError("json_record_list_required")
    for index, data in enumerate(rows):
        try:
            if not isinstance(data, dict) or not data:
                raise SealError("json_record_object_required")
            key = data[key_field]
            if not isinstance(key, (str, int)) or isinstance(key, bool) or not str(key).strip():
                raise SealError("record_key_missing")
            base = pointer + "/" + str(index)
            locators = {
                field: {"kind": "json", "pointer": base + "/" + pointer_part(field)}
                for field in data
            }
            key_locator = {**locators[key_field], "transform": "key_string"}
            detail_url = None
            if detail_url_field is not None:
                raw_url = data[detail_url_field]
                if not isinstance(raw_url, str):
                    raise SealError("record_detail_url_invalid")
                detail_url = public_url(response.urljoin(raw_url))
            yield record_item(
                response,
                record_type=record_type,
                record_key=str(key),
                data=data,
                locators=locators,
                key_locator=key_locator,
                detail_url=detail_url,
                schema_version=schema_version,
            )
        except (KeyError, TypeError, ValueError, SealError) as exc:
            yield diagnostic(
                response, exc.code if isinstance(exc, SealError) else "json_record_parse_failed"
            )


def is_attachment_response(response):
    """Identify the supported/static attachment representations for URL Recheck."""
    content_type = response.headers.get("Content-Type", b"").decode("latin1").lower()
    return any(
        t in content_type
        for t in (
            "application/pdf",
            "text/plain",
            "text/csv",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    ) or (
        urlsplit(response.url)
        .path.lower()
        .endswith((".pdf", ".txt", ".csv", ".xls", ".xlsx", ".docx"))
    )


def _attachment_record(
    response,
    *,
    parent=None,
    record_type="document_attachment",
    schema_version="record.v1",
    allow_partial=False,
):
    """Extract PDF/TXT/CSV/XLS/DOCX; failed inputs retain their original archive.

    The parent is a supplementary provenance input. Business content comes from
    the attachment itself, so a direct-URL Recheck produces identical data even
    when its parent is not fetched. Workbook rows locate evidence, never identity.
    """
    content_type = response.headers.get("Content-Type", b"").decode("latin1").lower()
    suffix = urlsplit(response.url).path.lower()
    locators = {}
    data = {}
    if suffix.endswith(".xls") or "application/vnd.ms-excel" in content_type:
        kind, field = "xls", "worksheets"
    elif suffix.endswith(".docx") or (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in content_type
    ):
        kind, field = "docx", "blocks"
    else:
        kind = None
    if kind:
        structure = parse_attachment(kind, response.body)
        title = attachment_projection(kind, structure, "title")
        body = attachment_projection(kind, structure, "text")
        data[field] = structure
        locators = {
            "title": {"kind": kind, "selection": "title"},
            "body": {"kind": kind, "selection": "text"},
            field: {"kind": kind, "selection": "structure"},
        }
        if kind == "docx":
            coverage = docx_coverage(response.body)
            if coverage["unparsed_elements"] or coverage["non_body_parts"]:
                if not allow_partial:
                    raise SealError("docx_unsupported_content")
                data["coverage"] = coverage
                locators["coverage"] = {"kind": kind, "selection": "coverage"}
    elif "application/pdf" in content_type or suffix.endswith(".pdf"):
        try:
            reader = PdfReader(BytesIO(response.body), strict=True)
            texts = [(page.extract_text() or "").strip() for page in reader.pages]
        except Exception:
            raise SealError("pdf_parse_failed") from None
        nonempty = [(number, text) for number, text in enumerate(texts, 1) if text]
        if not nonempty:
            raise SealError("pdf_text_layer_required")
        body = "\n".join(text for _, text in nonempty)
        locators["body"] = {
            "kind": "segments",
            "segments": [
                {"kind": "pdf", "page": number, "start": 0, "end": len(text)}
                for number, text in nonempty
            ],
        }
        first_page, first_text = nonempty[0]
        title = first_text.splitlines()[0].strip()
        locators["title"] = {
            "kind": "pdf",
            "page": first_page,
            "start": 0,
            "end": len(first_text.splitlines()[0]),
        }
    elif any(t in content_type for t in ("text/plain", "text/csv")) or suffix.endswith(
        (".txt", ".csv")
    ):
        # Octet-stream downloads may be binary Response objects even for .txt/.csv.
        # UTF-8 then matches the archive locator's explicit default encoding.
        text = (
            response.text if isinstance(response, TextResponse) else response.body.decode("utf-8")
        )
        body = text.strip()
        if not body:
            raise SealError("empty_document")
        locators["body"] = {"kind": "text", "start": 0, "end": len(text)}
        first_line = next(line for line in text.splitlines() if line.strip())
        start = text.index(first_line)
        title = first_line.strip()
        locators["title"] = {"kind": "text", "start": start, "end": start + len(first_line)}
    else:
        raise SealError("unsupported_content_type")
    supplementary = []
    if parent:
        supplementary = [{key: parent[key] for key in ("snapshot_id", "observation_id")}]
    url = public_url(response.url)
    return record_item(
        response,
        record_type=record_type,
        record_key=url,
        data={"title": title, "body": body, **data},
        locators=locators,
        key_locator={"kind": "response_url"},
        detail_url=url,
        schema_version=schema_version,
        supplementary_inputs=supplementary,
    )


def attachment_record(
    response, *, parent=None, record_type="document_attachment", schema_version="record.v1"
):
    """Return one complete attachment Record, rejecting unsupported DOCX content."""
    return _attachment_record(
        response, parent=parent, record_type=record_type, schema_version=schema_version
    )


def attachment_items(response, **kwargs):
    """Preserve supported DOCX text with explicit partial diagnostics.

    A reviewed Recipe must opt into this iterator for incomplete documents.
    Images, textboxes and non-body parts remain in immutable raw bytes; the
    coverage field proves their presence rather than claiming text extraction.
    """
    item = _attachment_record(response, allow_partial=True, **kwargs)
    yield item
    if "coverage" in item["data"]:
        yield diagnostic(response, "docx_unsupported_content")
