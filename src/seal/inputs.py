"""Every admitted content response follows archive -> parser -> document persistence."""

import asyncio
from datetime import datetime, timezone

from .archive import Component, request_key
from .core import SealError, digest, safe_url
from .db import connect, fenced, j, locked_run, record_error
from .discovery import mark_inputs_failed
from .documents import stage_document
from .helpers import is_attachment_response
from .resources import _stage_resource
from .scope import attachment_response_scope
from .seeded import is_sitemap_response


def register_input(context, response):
    request = response.request
    control = request.meta.get("seal_role") in {"robots", "sitemap"} or is_sitemap_response(
        response
    )
    content = not control and response.status == 200 and request.method in {"GET", "POST"}
    entry = {
        "key": request_key(request),
        "snapshot_id": request.meta["seal_snapshot_id"],
        "observation_id": request.meta["seal_observation_id"],
        "role": request.meta.get("seal_role", "aux"),
        "url": safe_url(response.url),
        "method": request.method,
        "logical_url": safe_url(request.meta.get("seal_logical_url", request.url)),
        "fetched_at": request.meta.get("seal_received_at"),
        "used_at": datetime.now(timezone.utc).isoformat(),
        "is_content": content,
    }
    if request.method == "POST":
        entry["request_body_sha256"] = digest(request.body)
        from .public_forms import representation_body

        entry["request_identity"] = digest(representation_body(context["config"], request))
    with connect() as c:
        source, run = locked_run(c, context["id"])
        fenced(source, run, context["attempt_epoch"])
        c.execute("UPDATE seal.run SET inputs=inputs || %s WHERE id=%s", (j([entry]), run["id"]))
        c.execute(
            "UPDATE seal.fetch_observation SET final_url=%s WHERE run_id=%s AND attempt_epoch=%s AND chain_id=%s",
            (entry["url"], run["id"], run["attempt_epoch"], request.meta.get("seal_chain_id")),
        )
        c.execute(
            "UPDATE seal.fetch_observation SET processing_stage=%s,is_content=%s WHERE id=%s",
            (
                "parsing" if content else "control" if control else "http_error",
                content,
                entry["observation_id"],
            ),
        )
        if content:
            run["inputs"].append(entry)
            _stage_resource(c, source, run, entry)
    return entry


def process_content(context, response, options):
    parent = response.meta.get("seal_parent_url")
    if parent and is_attachment_response(response):
        rejection = attachment_response_scope(context["config"], response.url, parent)
        if rejection:
            with connect() as c:
                c.execute(
                    "UPDATE seal.fetch_observation SET processing_stage='scope_failed',processing_error=%s WHERE id=%s",
                    (rejection, response.meta["seal_observation_id"]),
                )
            mark_inputs_failed(context, [response.meta["seal_snapshot_id"]], rejection)
            record_error(context["id"], context["attempt_epoch"], rejection)
            return
    entry = register_input(context, response)
    if not entry["is_content"]:
        return
    try:
        stage_document(
            context, {key: entry[key] for key in ("snapshot_id", "observation_id")}, options
        )
        with connect() as c:
            c.execute(
                "UPDATE seal.fetch_observation SET processing_stage='parsed' WHERE id=%s",
                (entry["observation_id"],),
            )
    except Exception as exc:
        code = exc.code if isinstance(exc, SealError) else "document_parse_failed"
        with connect() as c:
            c.execute(
                "UPDATE seal.fetch_observation SET processing_stage='parse_failed',processing_error=%s WHERE id=%s",
                (code, entry["observation_id"]),
            )
        mark_inputs_failed(context, [entry["snapshot_id"]], code)
        record_error(context["id"], context["attempt_epoch"], code)


class InputMiddleware(Component):
    async def process_response(self, request, response):
        response = response.replace(request=request)
        if request.meta.get("_seal_robots_policy"):
            return response
        # Adapter supplies format-specific options, never a content tree. A parse
        # failure is persisted before the independent discovery callback runs.
        spider = self.crawler.spider
        options = self.context["params"].get("options", {})
        try:
            if hasattr(spider, "parse_options"):
                options = spider.parse_options(response)
        except Exception:
            options = {"invalid_adapter_options": True}
        async with self.io:
            try:
                await asyncio.to_thread(process_content, self.context, response, options)
            except Exception as exc:
                code = exc.code if isinstance(exc, SealError) else "input_registration_failed"
                await asyncio.to_thread(
                    record_error, self.context["id"], self.context["attempt_epoch"], code
                )
        return response
