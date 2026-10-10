"""Robots reason semantics against real CLI/HTTP/PG and controlled fault injection."""

import argparse
import asyncio
import json
import socket
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from scrapy import Request
from scrapy.exceptions import IgnoreRequest
from scrapy.http import TextResponse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/v1.2-acceptance"))
from acceptance import Harness  # noqa: E402

from seal.archive import InputMiddleware, RequestGuard, ResponseArchiveMiddleware  # noqa: E402
from seal.completion import finish_run  # noqa: E402
from seal.core import SealError  # noqa: E402
from seal.db import connect, j  # noqa: E402
from seal.discovery import Discovery, ResourceFingerprinter  # noqa: E402
from seal.robots import ArchivedRobotsMiddleware  # noqa: E402
from seal.runs import begin_attempt, create_run, run_context  # noqa: E402


def injected_failure(h, binding, fault):
    """Real lifecycle/archive/Ledger with an isolated downloader or parser fault."""
    with patch.dict("os.environ", h.env):
        run_id = create_run(binding, "collect")
        epoch = begin_attempt(run_id)
        context = run_context(run_id, epoch)
        context["_seal_discovery_metadata"] = True
        crawler = SimpleNamespace(
            settings={"SEAL_CONTEXT": context},
            stats=Mock(),
            request_fingerprinter=ResourceFingerprinter(),
            spider=None,
        )
        extension = Discovery()
        extension.context, extension.crawler = context, crawler
        extension.enabled = extension.metadata_enabled = True
        extension.count, extension.limited = 0, False
        extension.query_variants, extension.known_fingerprints = {}, set()
        crawler._seal_discovery = extension
        guard = RequestGuard.from_crawler(crawler)
        archive = ResponseArchiveMiddleware.from_crawler(crawler)
        if fault == "guard":
            guard.count = context["config"]["budget"]["requests"]

        async def downloaded(request):
            await guard.process_request(request)
            headers = {"Content-Type": "text/plain"}
            if fault == "archive":
                headers["Content-Encoding"] = "gzip"
            response = TextResponse(
                request.url,
                body=b"User-agent: *\nAllow: /\nSitemap: /map.xml\n",
                headers=headers,
                request=request,
            )
            return await archive.process_response(request, response)

        crawler.engine = SimpleNamespace(download_async=AsyncMock(side_effect=downloaded))
        middleware = ArchivedRobotsMiddleware.__new__(ArchivedRobotsMiddleware)
        middleware.context, middleware.crawler = context, crawler
        middleware._parsers, middleware.reasons, middleware.sitemaps = {}, {}, {}
        middleware._stats = crawler.stats
        middleware._robotstxt_useragent = middleware._default_useragent = "SEAL"
        middleware.inputs = InputMiddleware.from_crawler(crawler)
        middleware._parse_robots = AsyncMock(side_effect=ValueError("controlled parser fault"))
        if fault == "input":
            middleware.inputs.process_spider_input = Mock(
                side_effect=SealError("controlled_input_failure")
            )
        business = Request(context["seeds"][0]["url"], meta={"seal_role": "detail"})
        extension.scheduled(business)
        try:
            asyncio.run(middleware.process_request(business))
        except IgnoreRequest as exc:
            h.check(fault + "_business_exception", str(exc), "robots_unavailable")
        else:
            raise AssertionError("Injected policy failure must block the business request")
        h.check(fault + "_failed_policy_sitemaps_absent", middleware.sitemaps, {})
        with connect() as c:
            c.execute(
                "UPDATE seal_run SET status='finishing',report=report || %s WHERE id=%s",
                (j({"finish_reason": "finished"}), run_id),
            )
        receipt = finish_run(run_id, epoch)
    inspection = h.cli("inspect", "run", run_id)
    h.capture("injected-" + fault, {"receipt": receipt, "inspect": inspection})
    events = inspection["discovery"]["events"]
    policy = next(e for e in events if e["role"] == "robots")
    reason = {
        "parser": "robots_unavailable",
        "input": "robots_unavailable",
        "guard": "request_budget_exceeded",
        "archive": "unsupported_content_encoding",
    }[fault]
    h.check(fault + "_policy_final_reason", policy["reason"], reason)
    h.check(fault + "_policy_terminal", policy["state"] in {"failed", "skipped"})
    h.check(fault + "_business_final_reason", events[0]["reason"], "robots_unavailable")
    h.check(
        fault + "_policy_archive_preserved",
        bool(policy["snapshot_id"]),
        fault in {"parser", "input"},
    )
    if fault in {"parser", "input"}:
        h.check(fault + "_policy_status_preserved", policy["response_status"], 200)
        h.check(fault + "_policy_observation_preserved", bool(policy["observation_id"]))
    if fault == "guard":
        h.check("guard_scope_decision_preserved", policy["scope_decision"], "limited")


def exercise(h):
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    scenario = ["allow"]

    def get(request):
        if request.path == "/robots.txt":
            if scenario[0] == "connection":
                h.site.ledger.append({"method": "GET", "path": request.path, "status": None})
                request.connection.shutdown(socket.SHUT_RDWR)
                request.connection.close()
                return
            status = int(scenario[0]) if scenario[0] in {"401", "403"} else 200
            body = {
                "401": b"Authentication required",
                "403": b"Domain forbidden",
                "rule": b"User-agent: *\nDisallow: /document\n",
                "challenge": b"<html><form>Sign in</form></html>",
            }.get(scenario[0], b"User-agent: *\nAllow: /\n")
            content_type = "text/plain"
        elif request.path == "/document":
            status, content_type = 200, "text/html; charset=utf-8"
            body = (
                "<html><h1>Robots policy test</h1><article><p>"
                "This public document contains sufficient text to form a valid deterministic "
                "record, provided its robots policy can be fetched and does not deny this URL."
                "</p></article></html>"
            ).encode()
        else:
            return original(request)
        h.site.ledger.append({"method": "GET", "path": request.path, "status": status})
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = get
    try:
        recipe = h.cli("recipe", "pack", "recipes/seeded")["recipe_version"]
        params = {"expand_homepage": False, "discover_sitemaps": False}
        for name, reason in (
            ("401", "robots_policy_http_denied"),
            ("403", "robots_policy_http_denied"),
            ("rule", "robots_denied"),
            ("challenge", "robots_unavailable"),
            ("connection", "robots_unavailable"),
        ):
            scenario[0] = name
            source = "robots_" + name
            h.config(
                source,
                entries=[h.site.url + "/document"],
                output_schema="record.v1",
                robots=True,
                delay=0.0,
            )
            binding = h.binding(source, recipe, params)
            before = len(h.site.ledger)
            receipt = h.cli("collect", source, "--binding", binding, ok=False)
            inspection = h.cli("inspect", "run", receipt["run_id"])
            h.capture(source, {"receipt": receipt, "inspect": inspection})
            paths = [row["path"] for row in h.site.ledger[before:]]
            h.check(source + "_business_not_downloaded", "/document" in paths, False)
            h.check(source + "_policy_fetch_once", paths.count("/robots.txt"), 1)
            h.check(source + "_blocked", receipt["summary"]["termination"], "blocked")
            h.check(source + "_run_reason", reason in receipt["errors"])
            events = inspection["discovery"]["events"]
            business = next(e for e in events if e["role"] != "robots")
            policy = next(e for e in events if e["role"] == "robots")
            h.check(source + "_business_reason", business["reason"], reason)
            h.check(source + "_policy_terminal", policy["state"] in {"parsed", "failed", "skipped"})
            h.check(source + "_policy_reason", policy["reason"], None if name == "rule" else reason)
            h.check(source + "_policy_archived", bool(policy["snapshot_id"]), name != "connection")
            if name == "connection":
                h.check(
                    "connection_observation_error_preserved",
                    bool(inspection["observations"][0]["error"]),
                )

        scenario[0] = "allow"
        for fault in ("parser", "input", "guard", "archive"):
            source = "injected_" + fault
            h.config(
                source,
                entries=[h.site.url + "/document"],
                output_schema="record.v1",
                robots=True,
                delay=0.0,
            )
            binding = h.binding(source, recipe, params)
            injected_failure(h, binding, fault)
    finally:
        handler.do_GET = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    h, error = Harness(args.output), None
    try:
        h.start()
        exercise(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("v1.3-robots-reasons", error)
        path = args.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(
            version="v1.3",
            command="uv run --frozen python experiments/v1.3-acceptance/robots_reasons_acceptance.py --output <new-directory>",
            scope="Real PostgreSQL/loopback HTTP reason contracts plus explicitly controlled parser/input/Guard/Archive fault injection",
        )
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        h.close()
    print(args.output.resolve())
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
