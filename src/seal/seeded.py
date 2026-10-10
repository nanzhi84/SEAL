"""Native link and sitemap parsing for an ordinary, reviewed seeded recipe."""

from urllib.parse import urlsplit, urlunsplit

from scrapy.linkextractors import LinkExtractor
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


def sitemap_links(response):
    try:
        sitemap = Sitemap(response.body)
        if sitemap.type not in {"sitemapindex", "urlset"}:
            raise SealError("sitemap_parse_failed")
        for entry in sitemap:
            if entry.get("loc"):
                yield entry["loc"], "sitemap" if sitemap.type == "sitemapindex" else "detail"
    except (ValueError, TypeError) as exc:
        raise SealError("sitemap_parse_failed") from exc


def robots_sitemaps(response):
    for line in response.body.decode("utf-8", errors="replace").splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == "sitemap" and value.strip():
            yield value.strip()


def is_sitemap_response(response):
    return response.meta.get("seal_role") == "sitemap" or (
        response.body.lstrip().startswith((b"<?xml", b"<urlset", b"<sitemapindex"))
        and b"<loc" in response.body
    )
