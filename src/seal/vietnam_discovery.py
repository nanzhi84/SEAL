"""Public ASP.NET document-list discovery for the registered Vietnam entry."""

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from lxml import html

from .core import SealError, atomic_write, canonical, digest, public_url
from .network import Resolver
from .scope import guard


def document(raw, url):
    tree = html.fromstring(raw)
    forms = tree.xpath('//form[@method="post" or @method="POST"]')
    grids = tree.xpath('//table[contains(@id,"grvDocument")]')
    if not forms or not grids:
        raise SealError("document_list_not_found")
    form, grid = forms[0], grids[0]
    fields = {}
    for node in form.xpath(".//input[@name] | .//select[@name] | .//textarea[@name]"):
        if node.tag == "select":
            options = node.xpath("./option[@selected]") or node.xpath("./option")[:1]
            fields[node.get("name")] = options[0].get("value", "") if options else ""
        elif node.get("type", "").lower() not in {"submit", "button", "image", "checkbox", "radio"}:
            fields[node.get("name")] = node.get("value", "")
    details = sorted(
        {
            public_url(urljoin(url, link))
            for link in grid.xpath(".//a/@href")
            if re.search(r"(?:\?|&)docid=\d+", link)
        }
    )
    events = []
    for link in grid.xpath(".//a/@href"):
        match = re.search(r"__doPostBack\('([^']+)','(Page\$[^']+)'\)", link)
        if match:
            events.append(match.groups())
    current = grid.xpath(".//tr[last()]//td/span/text()")
    if not details or not current or not str(current[-1]).strip().isdigit():
        raise SealError("document_page_identity_missing")
    number = int(current[-1])
    next_argument = f"Page${number + 1}"
    next_event = next((event for event in events if event[1] == next_argument), None)
    if next_event is None and any(event[1] == "Page$Last" for event in events):
        raise SealError("document_next_page_not_found")
    return {
        "number": number,
        "details": details,
        "fields": fields,
        "action": urljoin(url, form.get("action", "")),
        "next": next_event,
        "fingerprint": digest(details),
    }


def pdf_links(raw, url, config):
    links = set()
    for link in html.fromstring(raw).xpath("//a/@href"):
        target = urljoin(url, link)
        if not urlsplit(target).path.lower().endswith(".pdf"):
            continue
        try:
            links.add(guard(config, target))
        except SealError:
            raise SealError("pdf_link_out_of_scope") from None
    return sorted(links)


class Discovery:
    def __init__(self, config, database, output):
        self.config, self.db, self.output = config, database, output
        self.sem = asyncio.Semaphore(config["concurrency"])

    async def fetch(self, session, url, fields=None):
        # This POST only changes the public listing position, so replaying the same
        # form after a broken connection is safe. Bound retries; never skip a page.
        for attempt in range(3):
            try:
                return await self.fetch_once(session, url, fields)
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, TimeoutError):
                if attempt == 2:
                    raise SealError("discovery_connection_failed") from None
                await asyncio.sleep(0.5 * 2**attempt)

    async def fetch_once(self, session, url, fields=None):
        guard(self.config, url)
        async with self.sem:
            if self.config["delay"]:
                await asyncio.sleep(self.config["delay"])
            async with session.request(
                "POST" if fields else "GET", url, data=fields, allow_redirects=False
            ) as response:
                if response.status != 200:
                    raise SealError("discovery_http_" + str(response.status))
                if "text/html" not in response.headers.get("Content-Type", "").lower():
                    raise SealError("discovery_expected_html")
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > self.config["budget"]["response_bytes"]:
                        raise SealError("discovery_response_too_large")
                return bytes(body)

    def save_detail(self, url, raw):
        links = pdf_links(raw, url, self.config)
        ref = self.output / "details" / (digest(raw) + ".html")
        atomic_write(ref, raw)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO detail VALUES(?,?,?)", (url, str(ref), json.dumps(links))
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO pdf(url,status) VALUES(?,'pending')", [(u,) for u in links]
            )
        return {"url": url, "html_path": str(ref), "html_hash": digest(raw), "pdf_urls": links}

    async def details(self, session, urls):
        # Only one listing page is outstanding: bounded task count and memory.
        async def fetch_one(url):
            existing = self.db.execute("SELECT * FROM detail WHERE url=?", (url,)).fetchone()
            if existing:
                path = Path(existing["html_ref"])
                if digest(path.read_bytes()) != path.stem:
                    raise SealError("discovery_detail_evidence_corrupt")
                return {
                    "url": url,
                    "html_path": str(path),
                    "html_hash": path.stem,
                    "pdf_urls": json.loads(existing["pdf_urls"]),
                }
            return self.save_detail(url, await self.fetch(session, url))

        results = await asyncio.gather(*(fetch_one(url) for url in urls), return_exceptions=True)
        failure = next((r for r in results if isinstance(r, BaseException)), None)
        if failure:
            raise failure
        return results

    async def run(self, max_pages):
        saved = self.db.execute("SELECT * FROM page ORDER BY number").fetchall()
        if saved and max_pages and len(saved) > max_pages:
            raise SealError("harvest_page_scope_mismatch")
        complete = saved and (
            json.loads(saved[-1]["evidence"])["last_page"] or max_pages and len(saved) == max_pages
        )
        if complete:
            # A completed selection is frozen evidence, not today's moving index.
            for number, row in enumerate(saved, 1):
                evidence = json.loads(row["evidence"])
                if row["number"] != number:
                    raise SealError("discovery_page_evidence_corrupt")
                for item in [evidence, *evidence["details"]]:
                    if digest(Path(item["html_path"]).read_bytes()) != item["html_hash"]:
                        raise SealError("discovery_page_evidence_corrupt")
            return json.loads(saved[-1]["evidence"])["last_page"]
        # Public form POSTs can be disconnected on reused IIS connections. A fresh
        # transport succeeded for the same Page$6 payload during the live audit.
        connector = aiohttp.TCPConnector(
            limit=self.config["concurrency"], resolver=Resolver(), force_close=True
        )
        timeout = aiohttp.ClientTimeout(total=self.config["crawl4ai"]["page_timeout_ms"] / 1000)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": self.config["user_agent"]},
        ) as session:
            url = self.config["entry_urls"][0]
            raw = await self.fetch(session, url)
            previous, request = set(), {"method": "GET", "url": url}
            while True:
                page = document(raw, url)
                if page["fingerprint"] in previous:
                    raise SealError("document_pagination_repeated")
                previous.add(page["fingerprint"])
                if max_pages and page["number"] > max_pages:
                    break
                existing = self.db.execute(
                    "SELECT * FROM page WHERE number=?", (page["number"],)
                ).fetchone()
                if existing:
                    # A live source can shift while a long harvest is paused. Never silently
                    # combine old positional pages with a different new enumeration.
                    if existing["fingerprint"] != page["fingerprint"]:
                        raise SealError("document_listing_changed_start_new_state")
                details = await self.details(session, page["details"])
                path = self.output / f"page-{page['number']:04d}.html"
                evidence = {
                    "page": page["number"],
                    "request": request,
                    "html_path": str(path),
                    "html_hash": digest(raw),
                    "doc_urls": page["details"],
                    "details": details,
                    "pdf_urls": sorted({u for d in details for u in d["pdf_urls"]}),
                    "last_page": page["next"] is None,
                }
                atomic_write(path, raw)
                atomic_write(self.output / f"page-{page['number']:04d}.json", canonical(evidence))
                with self.db:
                    self.db.execute(
                        "INSERT OR REPLACE INTO page VALUES(?,?,?)",
                        (page["number"], page["fingerprint"], json.dumps(evidence)),
                    )
                print(
                    json.dumps(
                        {
                            "phase": "discovery",
                            "page": page["number"],
                            "documents": len(details),
                            "pdfs": len(evidence["pdf_urls"]),
                        }
                    ),
                    flush=True,
                )
                if page["next"] is None or max_pages and page["number"] == max_pages:
                    return page["next"] is None
                target, argument = page["next"]
                fields = dict(page["fields"], __EVENTTARGET=target, __EVENTARGUMENT=argument)
                request = {
                    "method": "POST",
                    "url": page["action"],
                    "event_target": target,
                    "event_argument": argument,
                }
                raw = await self.fetch(session, page["action"], fields)
                next_page = document(raw, url)
                if next_page["number"] != page["number"] + 1:
                    raise SealError("document_pagination_did_not_advance")
        return False
