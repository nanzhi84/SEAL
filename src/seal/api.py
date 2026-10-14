"""Bounded, read-only HTTP content service over existing SQL and S3 evidence."""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import psycopg
from aiohttp import web

from . import result_query
from .api_security import ApiError, Cursor, Settings, Tokens
from .core import SealError, public_url

log = logging.getLogger(__name__)
SERVICE = web.AppKey("service", object)


class Service:
    def __init__(self, settings):
        self.settings, self.tokens = settings, Tokens(settings)
        self.pool = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="seal-api")
        self.slots = asyncio.Semaphore(settings.workers)

    async def run(self, request, function, *args):
        request["waiting_for_slot"] = True
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=1)
        except TimeoutError:
            raise ApiError(503, "server_busy") from None
        request["waiting_for_slot"] = False
        try:
            future = asyncio.get_running_loop().run_in_executor(self.pool, partial(function, *args))
        except BaseException:
            self.slots.release()
            raise

        # A cancelled HTTP coroutine must not free capacity while its thread still runs.
        def finished(value):
            self.slots.release()
            if not value.cancelled():
                value.exception()  # Consume failures even after a disconnected/timed-out caller.

        future.add_done_callback(finished)
        return await asyncio.shield(future)


def storage_error(exc):
    if exc.code in {"archive_corrupt", "archive_invalid", "archive_missing", "archive_too_large"}:
        return ApiError(500, exc.code)
    if exc.code == "archive_read_timeout":
        return ApiError(503, "request_timeout")
    if exc.code in {"archive_unreadable", "object_storage_unavailable"}:
        return ApiError(503, "object_storage_unavailable")
    return ApiError(500, "internal_error")


@web.middleware
async def boundary(request, handler):
    route = request.match_info.route.name or "unmatched"
    error = None
    try:
        service = request.app[SERVICE]
        request["caller"] = service.settings.authenticate(request.headers.get("Authorization"))
        async with asyncio.timeout(service.settings.request_timeout_seconds):
            response = await handler(request)
    except ApiError as exc:
        error = exc
    except SealError as exc:
        error = storage_error(exc)
    except psycopg.Error:
        error = ApiError(503, "database_unavailable")
    except TimeoutError:
        # Admission and HTTP deadlines may expire in the same event-loop turn.
        # Report the actual phase instead of whichever timer happened to fire first.
        error = ApiError(
            503, "server_busy" if request.get("waiting_for_slot") else "request_timeout"
        )
    except web.HTTPException as exc:
        error = ApiError(exc.status, "method_not_allowed" if exc.status == 405 else "not_found")
    except Exception as exc:
        log.error("route=%s category=%s", route, type(exc).__name__)
        error = ApiError(500, "internal_error")
    if error:
        response = web.json_response({"error": error.code}, status=error.status)
        if error.status == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        log.warning("route=%s status=%s code=%s", route, error.status, error.code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def results(request):
    query = request.query
    try:
        if set(query) - {"source_url", "limit", "cursor"} or any(
            len(query.getall(key)) != 1 for key in query
        ):
            raise ValueError
        source_url = query["source_url"]
        if not source_url or len(source_url) > 8192:
            raise ValueError
        source_url = public_url(source_url)
        value = query.get("limit", "20")
        if not value.isascii() or not value.isdigit() or len(value) > 3:
            raise ValueError
        limit = int(value)
        if not 1 <= limit <= 100:
            raise ValueError
    except (ValueError, KeyError, SealError):
        raise ApiError(400, "invalid_parameters") from None
    service = request.app[SERVICE]
    cursor = service.tokens.decode(query["cursor"], Cursor) if "cursor" in query else None
    deadline = time.monotonic() + service.settings.request_timeout_seconds
    body, link = await service.run(
        request,
        result_query.results,
        service.settings,
        service.tokens,
        request["caller"],
        source_url,
        limit,
        cursor,
        deadline,
    )
    response = web.Response(body=body, content_type="application/json")
    if link:
        response.headers["Link"] = "<" + link + '>; rel="next"'
    return response


async def attachment(request):
    if request.query:
        raise ApiError(400, "invalid_parameters")
    service = request.app[SERVICE]
    body = await service.run(
        request,
        result_query.attachment,
        service.settings,
        service.tokens,
        request["caller"],
        request.match_info["attachment_id"],
        time.monotonic() + service.settings.request_timeout_seconds,
    )
    return web.Response(
        body=body,
        content_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="attachment"'},
    )


async def cleanup(app):
    # All HTTP tasks stop first; submitted work has bounded DB/S3 timeouts and deadlines.
    await asyncio.to_thread(app[SERVICE].pool.shutdown, wait=True, cancel_futures=True)


class ProtocolLog(logging.Filter):
    def filter(self, record):
        category = type(record.exc_info[1]).__name__ if record.exc_info else "ProtocolError"
        record.msg, record.args = "protocol_error category=%s", (category,)
        record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


def application(settings):
    app = web.Application(middlewares=[boundary], client_max_size=1024)
    app[SERVICE] = Service(settings)
    app.router.add_get("/results", results, name="results", allow_head=False)
    app.router.add_get(
        "/attachments/{attachment_id}", attachment, name="attachment", allow_head=False
    )
    app.on_cleanup.append(cleanup)
    return app


def serve(path, host, port):
    from .db import dsn
    from .migration import require_current
    from .objects import Objects

    if not path:
        raise SealError("SEAL_API_CONFIG_required")
    if not 1 <= port <= 65535:
        raise SealError("invalid_arguments")
    settings = Settings.load(path)
    dsn()
    Objects().check()
    require_current()
    logging.basicConfig(level=logging.WARNING, format="%(name)s %(message)s")
    protocol_logger = logging.getLogger("seal.api.protocol")
    protocol_logger.addFilter(ProtocolLog())
    web.run_app(
        application(settings),
        host=host,
        port=port,
        print=None,
        access_log=None,
        handler_cancellation=True,
        shutdown_timeout=65,
        max_line_size=16384,
        max_field_size=8192,
        logger=protocol_logger,
    )
    return {"status": "stopped", "service": "results-api"}
