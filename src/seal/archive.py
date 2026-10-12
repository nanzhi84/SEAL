"""Store decoded application responses before native redirects and retries."""

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from scrapy.exceptions import IgnoreRequest
from scrapy.http import HtmlResponse, JsonResponse, Response, TextResponse, XmlResponse

from .core import (
    Objects,
    SealError,
    check_address,
    digest,
    public_url,
    safe_url,
    uid,
)
from .db import connect, fenced, locked_run, record_error
from .discovery import ResourceFingerprinter, mark_archived, mark_failed, mark_requested
from .public_forms import check_public_form
from .scope import paths_for, robots_policy_request

HEADERS = {
    b"content-type",
    b"content-language",
    b"etag",
    b"last-modified",
    b"location",
    b"retry-after",
}
TYPES = {
    cls.__name__: cls for cls in (Response, HtmlResponse, JsonResponse, TextResponse, XmlResponse)
}


def request_key(request):
    if "seal_request_key" in request.meta:
        return request.meta["seal_request_key"]
    return digest(
        {
            "url": safe_url(request.meta.get("seal_logical_url", request.url)),
            "method": request.method,
            "body": digest(request.body),
            "role": request.meta.get("seal_role", "aux"),
            "headers": {
                k: request.headers.get(k, b"").decode("latin1")
                for k in ("Accept", "Accept-Language")
            },
        }
    )


def archive_response(context, request, response):
    if response.flags and "cached" in response.flags:
        raise SealError("cache_provenance_unknown")
    if response.headers.get(b"Content-Encoding"):
        raise SealError("unsupported_content_encoding")
    objects = Objects()
    body_hash = objects.put(response.body)
    headers = {
        k.decode("latin1"): [v.decode("latin1") for v in values]
        for k, values in response.headers.items()
        if k.lower() in HEADERS
    }
    for key in list(headers):
        if key.lower() == "location":
            headers[key] = [safe_url(response.urljoin(v)) for v in headers[key]]
    snapshot = {
        "contract": 1,
        "body_hash": body_hash,
        "body_size": len(response.body),
        "request_url": safe_url(request.url),
        "representation_id": ResourceFingerprinter().fingerprint(request).hex(),
        "method": request.method,
        "url": safe_url(response.url),
        "status": response.status,
        "headers": headers,
        "response_type": type(response).__name__,
        "encoding": response.encoding if isinstance(response, TextResponse) else None,
    }
    snapshot_id = objects.put_json(snapshot)
    observation = request.meta["seal_request_id"]
    with connect() as c:
        c.execute(
            """INSERT INTO seal.fetch_observation
          (id,source_id,run_id,attempt_epoch,request_id,request_key,method,url,status,snapshot_id,body_hash,requested_at,fetched_at,parent_id,chain_id,processing_stage)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(request_id) DO NOTHING""",
            (
                observation,
                context["source_id"],
                context["id"],
                context["attempt_epoch"],
                observation,
                request_key(request),
                request.method,
                safe_url(request.url),
                response.status,
                snapshot_id,
                body_hash,
                request.meta["seal_requested_at"],
                request.meta["seal_received_at"],
                request.meta.get("seal_parent_id"),
                request.meta["seal_chain_id"],
                "redirect"
                if 300 <= response.status < 400
                else "http_error"
                if response.status != 200
                else "archived",
            ),
        )
    return snapshot_id, observation


class Component:
    @classmethod
    def from_crawler(cls, crawler):
        value = cls()
        value.crawler = crawler
        value.context = crawler.settings["SEAL_CONTEXT"]
        value.config = value.context["config"]
        value.io = asyncio.Semaphore(value.config["concurrency"])
        return value

    async def fail(self, code):
        self.crawler.stats.inc_value("seal/errors")
        await asyncio.to_thread(
            record_error, self.context["id"], self.context["attempt_epoch"], code
        )


class RequestGuard(Component):
    def __init__(self):
        self.count = 0

    async def process_request(self, request):
        try:
            url = public_url(request.url)
            parsed = urlsplit(url)
            policy = robots_policy_request(self.config, request)
            if request.meta.get("_seal_robots_policy") and not policy:
                raise SealError("robots_policy_redirect_rejected")
            if not policy and not paths_for(self.config, parsed.hostname, parsed.path):
                raise SealError("request_out_of_scope")
            parent = request.meta.get("seal_parent_url")
            if parent and request.meta.get("seal_role") in {"iframe", "attachment"}:
                parent_host = urlsplit(public_url(parent)).hostname
                if parsed.hostname != parent_host:
                    if request.meta["seal_role"] == "iframe":
                        raise SealError("iframe_out_of_scope")
                    if parsed.hostname not in self.config.get("host_path_scopes", {}):
                        raise SealError("attachment_host_path_required")
            check_address(parsed.hostname)
            if request.method not in self.config["methods"]:
                raise SealError("request_method_rejected")
            if request.method == "POST":
                if any(rule["url"] == url for rule in self.config.get("public_html_forms", [])):
                    from .html_forms import check_html_form

                    await asyncio.to_thread(check_html_form, self.config, self.context, request)
                else:
                    check_public_form(
                        self.config, url, request.body, request.headers.get("Content-Type", b"")
                    )
            elif request.body:
                raise SealError("request_method_rejected")
            if any(
                request.headers.get(k)
                for k in (
                    "Authorization",
                    "Proxy-Authorization",
                    "Cookie",
                    "If-None-Match",
                    "If-Modified-Since",
                )
            ):
                raise SealError("request_sensitive_or_conditional_header")
            self.count += 1
            if self.count > self.config["budget"]["requests"]:
                raise SealError("request_budget_exceeded")
            if datetime.now(timezone.utc) >= datetime.fromisoformat(self.context["deadline"]):
                raise SealError("deadline_exceeded")
            request.meta["seal_parent_id"] = request.meta.pop("seal_observation_id", None)
            request.meta.pop("seal_snapshot_id", None)
            request.meta["seal_request_id"] = uid()
            request.meta.setdefault("seal_chain_id", uid())
            request.meta.setdefault("seal_logical_url", url)
            request.meta.setdefault("seal_request_key", request_key(request))
            request.meta["seal_requested_at"] = datetime.now(timezone.utc).isoformat()
            request.meta["seal_scope_decision"] = "policy" if policy else "allowed"
            await asyncio.to_thread(mark_requested, self.context, request)
        except SealError as exc:
            await asyncio.to_thread(mark_failed, self.context, request, exc.code)
            await self.fail(exc.code)
            raise IgnoreRequest(exc.code) from None


class ResponseArchiveMiddleware(Component):
    async def process_response(self, request, response):
        request.meta["seal_received_at"] = datetime.now(timezone.utc).isoformat()
        try:
            async with self.io:
                snapshot, observation = await asyncio.to_thread(
                    archive_response, self.context, request, response
                )
            request.meta["seal_snapshot_id"] = snapshot
            request.meta["seal_observation_id"] = observation
            await asyncio.to_thread(
                mark_archived, self.context, request, snapshot, observation, response.status
            )
            if response.status == 429:
                until = datetime.now(timezone.utc) + timedelta(hours=1)
                raw = response.headers.get("Retry-After", b"").decode("ascii", "ignore")
                try:
                    until = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=max(60, min(int(raw), 86400)))
                        if raw.isdigit()
                        else parsedate_to_datetime(raw)
                    )
                except (TypeError, ValueError):
                    pass
                await asyncio.to_thread(self.cooldown, until)
                await self.fail("source_rate_limited")
                asyncio.create_task(
                    self.crawler.engine.close_spider_async(reason="source_rate_limited")
                )
            if response.status == 304:
                await self.fail("unexpected_304")
            return response
        except Exception as exc:
            code = exc.code if isinstance(exc, SealError) else "archive_failed"
            await asyncio.to_thread(mark_failed, self.context, request, code)
            await self.fail(code)
            raise IgnoreRequest(code) from None

    def cooldown(self, until):
        with connect() as c:
            source, run = locked_run(c, self.context["id"])
            try:
                fenced(source, run, self.context["attempt_epoch"])
            except SealError:
                return
            c.execute(
                "UPDATE seal.source SET cooldown_until=%s WHERE id=%s",
                (until, self.context["source_id"]),
            )

    async def process_exception(self, request, exception):
        if isinstance(exception, IgnoreRequest):
            return None
        async with self.io:
            await asyncio.to_thread(self.network_error, request, type(exception).__name__)
            await asyncio.to_thread(mark_failed, self.context, request, "download_failed")
        return None

    def network_error(self, request, code):
        observation = request.meta.get("seal_request_id", uid())
        with connect() as c:
            c.execute(
                """INSERT INTO seal.fetch_observation(id,source_id,run_id,attempt_epoch,request_id,request_key,method,url,requested_at,error,chain_id,processing_stage,processing_error)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'fetch_failed',%s) ON CONFLICT DO NOTHING""",
                (
                    observation,
                    self.context["source_id"],
                    self.context["id"],
                    self.context["attempt_epoch"],
                    observation,
                    request_key(request),
                    request.method,
                    safe_url(request.url),
                    request.meta.get("seal_requested_at", datetime.now(timezone.utc)),
                    code,
                    request.meta.get("seal_chain_id"),
                    code,
                ),
            )
