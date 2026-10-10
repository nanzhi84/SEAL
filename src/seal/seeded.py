"""Native link and sitemap parsing for an ordinary, reviewed seeded recipe."""

import struct
import zlib
from gzip import GzipFile
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

from lxml.etree import XMLSyntaxError
from scrapy.linkextractors import LinkExtractor
from scrapy.utils._compression import (
    _CHUNK_SIZE,
    _check_max_size,
    _DecompressionMaxSizeExceeded,
)
from scrapy.utils.gz import gzip_magic_number
from scrapy.utils.sitemap import Sitemap

from .core import SealError

ATTACHMENT_EXTENSIONS = {"pdf", "txt", "csv", "xls", "xlsx", "doc", "docx"}
PRESENTATION_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "svg",
    "ico",
    "css",
    "js",
    "woff",
    "woff2",
    "ttf",
    "mp3",
    "mp4",
    "zip",
    "exe",
}


def origin_url(url, path):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def html_links(response):
    # Do not deduplicate or canonicalize in extraction: every original candidate
    # reaches the ledger and Scrapy alone deduplicates identical wire resources.
    extractor = LinkExtractor(unique=False, canonicalize=False, deny_extensions=[])
    for link in extractor.extract_links(response):
        suffix = urlsplit(link.url).path.rsplit(".", 1)[-1].lower()
        if suffix in PRESENTATION_EXTENSIONS:
            continue
        role = "attachment" if suffix in ATTACHMENT_EXTENSIONS else "detail"
        method = (
            "attachment"
            if role == "attachment"
            else "pagination"
            if ("next" in link.text.lower() or "下一" in link.text or "下页" in link.text)
            else "html"
        )
        yield link.url, role, method
    for node in response.css("iframe[src]"):
        yield response.urljoin(node.attrib["src"]), "iframe", "iframe"
    for node in response.css('link[rel="sitemap"][href]'):
        yield response.urljoin(node.attrib["href"]), "sitemap", "sitemap"


def sitemap_body(response, *, max_size=10485760):
    """Read a sitemap without changing the archived download representation.

    Use Scrapy's chunk size and decompression bound, but require the complete
    gzip stream and its checksum. Scrapy's general-purpose ``gunzip`` tolerates
    truncated or corrupt streams; a partial sitemap must not silently omit URLs.
    HTTP Content-Encoding may already have been decoded by Scrapy. Inspect the
    actual bytes rather than relying on the filename or header in that case.
    """
    if not isinstance(max_size, int) or isinstance(max_size, bool) or max_size <= 0:
        raise SealError("sitemap_size_limit_invalid")
    body = response.body
    if not gzip_magic_number(response) and not body.startswith(b"\x1f\x8b"):
        if len(body) > max_size:
            raise SealError("sitemap_size_exceeded")
        return body
    output = BytesIO()
    try:
        with GzipFile(fileobj=BytesIO(body)) as stream:
            while chunk := stream.read1(_CHUNK_SIZE):
                _check_max_size(output.tell() + len(chunk), max_size)
                output.write(chunk)
    except _DecompressionMaxSizeExceeded as exc:
        raise SealError("sitemap_size_exceeded") from exc
    except (OSError, EOFError, struct.error, zlib.error) as exc:
        raise SealError("sitemap_decompression_failed") from exc
    return output.getvalue()


def sitemap_links(response, *, max_size=10485760):
    try:
        sitemap = Sitemap(sitemap_body(response, max_size=max_size))
        if sitemap.type not in {"sitemapindex", "urlset"}:
            raise SealError("sitemap_parse_failed")
        for entry in sitemap:
            if entry.get("loc"):
                yield entry["loc"], "sitemap" if sitemap.type == "sitemapindex" else "detail"
    except (ValueError, TypeError, XMLSyntaxError, StopIteration) as exc:
        raise SealError("sitemap_parse_failed") from exc


def robots_sitemaps(response):
    for line in response.body.decode("utf-8", errors="replace").splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "sitemap" and value.strip():
            yield value.strip()


def is_sitemap_response(response):
    return (
        response.meta.get("seal_role") == "sitemap"
        or urlsplit(response.url).path.lower().endswith(".xml.gz")
    ) or (
        response.body.lstrip().startswith((b"<?xml", b"<urlset", b"<sitemapindex"))
        and b"<loc" in response.body
    )
