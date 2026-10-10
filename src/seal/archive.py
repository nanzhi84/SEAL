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
from .db import connect, fenced, j, locked_run, record_error
from .discovery import ResourceFingerprinter, mark_archived, mark_failed, mark_requested
from .scope import paths_for

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


def restore(snapshot_id, request):
    objects = Objects()
    snapshot = objects.json(snapshot_id)
    body = objects.get(snapshot["body_hash"])
    cls = TYPES.get(snapshot["response_type"])
    if cls is None:
        raise SealError("unsupported_response_type")
    kwargs = {"encoding": snapshot["encoding"]} if issubclass(cls, TextResponse) else {}
    return cls(
        url=snapshot["url"],
        status=snapshot["status"],
        headers=snapshot["headers"],
        body=body,
        request=request,
        **kwargs,
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
            """INSERT INTO seal_fetch_observation
          (id,source_id,run_id,attempt_epoch,request_id,request_key,method,url,status,snapshot_id,body_hash,requested_at,fetched_at,parent_id,chain_id)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(request_id) DO NOTHING""",
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
            if not paths_for(self.config, parsed.hostname, parsed.path):
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
            if request.method not in self.config["methods"] or request.body:
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
            await asyncio.to_thread(mark_requested, self.context, request)
        except SealError as exc:
            await asyncio.to_thread(mark_failed, self.context, request, exc.code)
            await self.fail(exc.code)
            raise IgnoreRequest(exc.code) from None


class ResponseArchiveMiddleware(Component):
    async def process_response(self, request, response):
        if self.context["mode"] == "replay":
            async with self.io:
                await asyncio.to_thread(Objects().get, request.meta["seal_snapshot_id"])
            return response
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
        if self.context["mode"] != "replay":
            with connect() as c:
                source, run = locked_run(c, self.context["id"])
                try:
                    fenced(source, run, self.context["attempt_epoch"])
                except SealError:
                    return  # Late observations cannot change current Source controls.
                c.execute(
                    "UPDATE seal_source SET cooldown_until=%s WHERE id=%s",
                    (until, self.context["source_id"]),
                )

    async def process_exception(self, request, exception):
        if self.context["mode"] == "replay":
            if not isinstance(exception, IgnoreRequest):
                await self.fail(
                    exception.code if isinstance(exception, SealError) else "replay_restore_failed"
                )
            return None
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
                """INSERT INTO seal_fetch_observation(id,source_id,run_id,attempt_epoch,request_id,request_key,method,url,requested_at,error,chain_id)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
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
                ),
            )


class ReplayMiddleware(Component):
    async def process_request(self, request):
        matches = [
            item for item in self.context["replay_inputs"] if item["key"] == request_key(request)
        ]
        unique = {item["snapshot_id"] for item in matches}
        if len(unique) != 1:
            code = "replay_miss" if not unique else "replay_ambiguous"
            await asyncio.to_thread(mark_failed, self.context, request, code)
            await self.fail(code)
            raise IgnoreRequest(code)
        entry = matches[0]
        request.meta["seal_snapshot_id"] = entry["snapshot_id"]
        request.meta["seal_observation_id"] = entry["observation_id"]
        request.meta["seal_original_fetched_at"] = entry["fetched_at"]
        async with self.io:
            await asyncio.to_thread(mark_requested, self.context, request, replayed=True)
            response = await asyncio.to_thread(restore, entry["snapshot_id"], request)
            await asyncio.to_thread(
                mark_archived,
                self.context,
                request,
                entry["snapshot_id"],
                entry["observation_id"],
                response.status,
            )
            return response


class InputMiddleware(Component):
    def process_spider_input(self, response):
        request = response.request
        entry = {
            "key": request_key(request),
            "snapshot_id": request.meta["seal_snapshot_id"],
            "observation_id": request.meta["seal_observation_id"],
            "role": request.meta.get("seal_role", "aux"),
            "url": safe_url(response.url),
            "method": request.method,
            "logical_url": safe_url(request.meta.get("seal_logical_url", request.url)),
            "fetched_at": request.meta.get(
                "seal_original_fetched_at", request.meta.get("seal_received_at")
            ),
            "used_at": datetime.now(timezone.utc).isoformat(),
        }
        # Only tiny manifest/SQL work here; body I/O has already completed in Archive.
        with connect() as c:
            accepted = c.execute(
                "UPDATE seal_run SET inputs=inputs || %s WHERE id=%s AND attempt_epoch=%s "
                "AND status IN ('running','finishing') RETURNING id",
                (j([entry]), self.context["id"], self.context["attempt_epoch"]),
            ).fetchone()
            if accepted is None:
                raise SealError("inactive_attempt")
            if self.context["mode"] != "replay":
                c.execute(
                    "UPDATE seal_fetch_observation SET final_url=%s WHERE run_id=%s AND attempt_epoch=%s AND chain_id=%s",
                    (
                        entry["url"],
                        self.context["id"],
                        self.context["attempt_epoch"],
                        request.meta.get("seal_chain_id"),
                    ),
                )
        if self.config.get("output_schema") == "record.v1" and response.status == 200:
            from .records import stage_record_resource

            stage_record_resource(self.context, entry)
