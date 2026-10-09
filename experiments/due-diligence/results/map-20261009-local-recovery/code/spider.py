"""Bounded map probes through Scrapy's native downloader; no production DB."""

import hashlib
import ipaddress
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import scrapy
from classify import extract, is_robots_document
from inventory import clean_url, write_json
from protego import Protego
from scrapy import signals
from scrapy.exceptions import IgnoreRequest

from seal.core import BODY_SECRET

UA = "SEAL-SourceExperiment/1.0 (bounded public-source feasibility)"


def now():
    return datetime.now(timezone.utc).isoformat()


def origin(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "", "", ""))


def error_types(error):
    """Record causal classes/errno only: exception messages may contain secrets."""
    causes, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        causes.append({"type": type(error).__name__, "errno": getattr(error, "errno", None)})
        error = error.__cause__ or error.__context__
    return causes


class Ledger:
    @classmethod
    def from_crawler(cls, crawler):
        value = cls()
        value.crawler = crawler
        value.count = 0
        return value

    def process_request(self, request):
        if self.count >= 2400:
            raise IgnoreRequest("campaign_request_budget_exhausted")
        host = urlsplit(request.url).hostname
        if host in {"localhost", "localhost.localdomain"}:
            raise IgnoreRequest("non_public_destination")
        try:
            if not ipaddress.ip_address(host).is_global:
                raise IgnoreRequest("non_public_destination")
        except ValueError:
            pass
        self.count += 1
        request.meta["request_id"] = str(uuid4())
        request.meta["sent_at"] = now()
        request.meta["sent_clock"] = time.monotonic()
        record = {
            "request_id": request.meta["request_id"],
            "url": clean_url(request.url),
            "method": request.method,
            "kind": request.meta["kind"],
            "sent_at": request.meta["sent_at"],
        }
        with (self.crawler.spider.root / "requests.jsonl").open("a") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


class MapSpider(scrapy.Spider):
    name = "due_diligence_map"

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.spider_error, signal=signals.spider_error)
        return spider

    def spider_error(self, failure, response, spider):
        with (self.root / "spider-errors.jsonl").open("a") as file:
            file.write(
                json.dumps({"url": clean_url(response.url), "causes": error_types(failure.value)})
                + "\n"
            )

    def __init__(self, rows, root, **kwargs):
        super().__init__(**kwargs)
        self.rows, self.root = rows, Path(root)
        self.pending, self.policy, self.results, self.exchanges = {}, {}, {}, []
        self.destinations = {}

    def emit(self, row, values):
        self.results[row["id"]] = dict(row, **values)
        with (self.root / "results.jsonl").open("a") as file:
            file.write(json.dumps(self.results[row["id"]], ensure_ascii=False) + "\n")
        if len(self.results) % 25 == 0:
            print(
                json.dumps(
                    {"completed_rows": len(self.results), "total": len(self.rows)},
                    ensure_ascii=False,
                ),
                flush=True,
            )

    def request(self, url, kind, callback, meta):
        return scrapy.Request(
            url,
            headers={"Connection": "close"},
            callback=callback,
            errback=self.failed,
            dont_filter=True,
            meta={**meta, "kind": kind, "handle_httpstatus_all": True},
        )

    def route(self, url, rows, chain=None):
        chain = chain or []
        key = origin(url)
        task = {"url": url, "rows": rows, "chain": chain}
        if key in self.policy:
            yield from self.content_if_allowed(task)
        else:
            first = key not in self.pending
            self.pending.setdefault(key, []).append(task)
            if first:
                yield self.request(
                    key + "/robots.txt", "robots", self.robots, {"origin": key, "hops": 0}
                )

    async def start(self):
        unique = {}
        for row in self.rows:
            unique.setdefault(row["url"], []).append(row)
        for url, rows in unique.items():
            for request in self.route(url, rows):
                yield request

    def observe(self, response):
        record = {
            "request_id": response.meta["request_id"],
            "url": clean_url(response.url),
            "status": response.status,
            "kind": response.meta["kind"],
            "fetched_at": now(),
            "sent_at": response.meta["sent_at"],
            "bytes": len(response.body),
            "body_sha256": hashlib.sha256(response.body).hexdigest(),
            "elapsed_seconds": round(time.monotonic() - response.meta["sent_clock"], 3),
            "content_type": response.headers.get("Content-Type", b"").decode("latin1"),
            "location": clean_url(response.urljoin(response.headers["Location"].decode("latin1")))
            if response.headers.get("Location")
            else None,
        }
        self.exchanges.append(record)
        with (self.root / "responses.jsonl").open("a") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def robots(self, response):
        observed = self.observe(response)
        key = response.meta["origin"]
        if (
            response.status in (301, 302, 303, 307, 308)
            and observed["location"]
            and response.meta["hops"] < 4
        ):
            yield self.request(
                observed["location"],
                "robots",
                self.robots,
                {"origin": key, "hops": response.meta["hops"] + 1},
            )
            return
        body = response.body.decode("utf-8", "replace")
        if response.status in (404, 410):
            policy = {"status": "absent", "allowed": True, "robots_observation": observed}
        elif response.status == 200 and is_robots_document(response.body):
            policy = {
                "status": "parsed",
                "allowed": True,
                "parser": Protego.parse(body),
                "robots_observation": observed,
            }
            path = self.root / "robots" / (hashlib.sha256(key.encode()).hexdigest() + ".txt")
            path.parent.mkdir(exist_ok=True)
            if not BODY_SECRET.search(response.body):
                path.write_bytes(response.body)
                policy["robots_path"] = str(path.relative_to(self.root))
        else:
            policy = {"status": "unknown", "allowed": False, "robots_observation": observed}
        self.policy[key] = policy
        for task in self.pending.pop(key, []):
            yield from self.content_if_allowed(task)

    def content_if_allowed(self, task):
        policy = self.policy[origin(task["url"])]
        allowed = policy["allowed"] and (
            "parser" not in policy or policy["parser"].can_fetch(task["url"], UA)
        )
        if not allowed:
            for row in task["rows"]:
                self.emit(
                    row,
                    {
                        "outcome": "ROBOTS_DENIED" if policy["allowed"] else "ROBOTS_UNKNOWN",
                        "reason": "ROBOTS_DISALLOW"
                        if policy["allowed"]
                        else "ROBOTS_UNAVAILABLE_OR_INVALID",
                        "robots": {k: v for k, v in policy.items() if k != "parser"},
                        "chain": task["chain"],
                        "http_status": None,
                        "content_attempted": False,
                        "business_verified": False,
                    },
                )
            return
        if len(task["chain"]) > 4:
            for row in task["rows"]:
                self.emit(
                    row,
                    {
                        "outcome": "REDIRECT_LIMIT",
                        "reason": "MORE_THAN_4_REDIRECTS",
                        "chain": task["chain"],
                    },
                )
            return
        yield self.request(task["url"], "entry", self.entry, {"task": task})

    def entry(self, response):
        observed = self.observe(response)
        task = response.meta["task"]
        chain = task["chain"] + [observed]
        policy = self.policy[origin(response.url)]
        robots = {k: v for k, v in policy.items() if k != "parser"}
        if response.status in (301, 302, 303, 307, 308) and observed["location"]:
            if any(hop["url"] == observed["location"] for hop in chain):
                for row in task["rows"]:
                    self.emit(
                        row, {"outcome": "REDIRECT_LOOP", "reason": "REDIRECT_LOOP", "chain": chain}
                    )
            else:
                yield from self.route(observed["location"], task["rows"], chain)
            return
        try:
            result = extract(response)
        except Exception as exc:
            result = {"outcome": "PARSE_ERROR", "reason": type(exc).__name__}
        result.update(
            http_status=response.status,
            final_url=clean_url(response.url),
            chain=chain,
            robots=robots,
            content_attempted=True,
            fetched_at=observed["fetched_at"],
            body_sha256=observed["body_sha256"],
            encoding=getattr(response, "encoding", None),
        )
        if result["outcome"].startswith("ACCESSIBLE") or result["outcome"] in {
            "SKIP_INPUT",
            "JS_SHELL",
        }:
            if BODY_SECRET.search(response.body):
                result["archive_status"] = "SENSITIVE_PATTERN_NOT_ARCHIVED"
                result.pop("excerpt", None)
            else:
                path = self.root / "bodies" / observed["body_sha256"]
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(response.body)
                result.update(raw_path=str(path.relative_to(self.root)), archive_status="ARCHIVED")
        else:
            result.pop("excerpt", None)
        for row in task["rows"]:
            self.emit(row, result)

    def failed(self, failure):
        request = failure.request
        record = {
            "request_id": request.meta.get("request_id"),
            "url": clean_url(request.url),
            "kind": request.meta["kind"],
            "error": failure.type.__name__,
            "causes": error_types(failure.value),
            "at": now(),
        }
        with (self.root / "errors.jsonl").open("a") as file:
            file.write(json.dumps(record) + "\n")
        if request.meta["kind"] == "robots":
            key = request.meta["origin"]
            self.policy[key] = {"status": "unknown", "allowed": False, "error": record}
            for task in self.pending.pop(key, []):
                yield from self.content_if_allowed(task)
        else:
            task = request.meta["task"]
            for row in task["rows"]:
                self.emit(
                    row,
                    {
                        "outcome": "NETWORK_ERROR",
                        "reason": failure.type.__name__,
                        "error": record,
                        "chain": task["chain"],
                        "http_status": None,
                        "content_attempted": True,
                        "business_verified": False,
                    },
                )

    def closed(self, reason):
        for row in self.rows:
            if row["id"] not in self.results:
                self.emit(row, {"outcome": "INTERRUPTED", "reason": reason})
        write_json(
            self.root / "results.json", list(sorted(self.results.values(), key=lambda r: r["id"]))
        )
