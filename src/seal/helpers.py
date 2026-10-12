"""Source adapter helpers; scheduling, archive and parsing belong to Runtime."""

from urllib.parse import urlsplit

import scrapy


def input_reference(response):
    return {
        "snapshot_id": response.meta["seal_snapshot_id"],
        "observation_id": response.meta["seal_observation_id"],
    }


def diagnostic(response, code):
    """Attribute parse failures to archived inputs, including per-row failures."""
    return {"type": "diagnostic", "code": code, **input_reference(response)}


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


def is_attachment_response(response):
    """Identify attachment responses, including dynamic download endpoints."""
    content_type = response.headers.get("Content-Type", b"").decode("latin1").lower()
    return any(
        t in content_type
        for t in (
            "application/pdf",
            "text/plain",
            "text/csv",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    ) or (
        urlsplit(response.url)
        .path.lower()
        .endswith((".pdf", ".txt", ".csv", ".xls", ".xlsx", ".docx"))
    )
