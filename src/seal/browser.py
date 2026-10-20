"""rendered_dom acquisition: dedicated headless browser, scoped and budgeted subrequests.

The archived representation is the rendered DOM UTF-8 handed to the parser, never claimed
to be an original HTTP response.
"""

import asyncio

from .core import SealError
from .scope import guard
from .transport import request_key


class Browser:
    def __init__(self, config, budget, recorder, transport):
        self.config, self.budget, self.recorder, self.transport = (
            config,
            budget,
            recorder,
            transport,
        )
        self.playwright = None
        self.browser = None
        self.errors = []

    async def start(self):
        if self.browser is None:
            from playwright.async_api import async_playwright

            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)

    async def close(self):
        if self.browser is not None:
            await self.browser.close()
        if self.playwright is not None:
            await self.playwright.stop()

    async def acquire(self, request):
        await self.start()
        await self.transport.start()
        url = guard(self.config, request["url"])
        await self.transport.robots.check(url, self.transport.fetch_policy)
        self.budget.request()
        key = request_key(request)
        identity = self.recorder.start(
            key, "rendered_dom", "content", "GET", url, {"role": request.get("role", "page")}
        )
        context = await self.browser.new_context(
            user_agent=self.config["user_agent"], accept_downloads=False, ignore_https_errors=False
        )
        try:
            page = await context.new_page()
            session = await context.new_cdp_session(page)

            async def paused(event):
                request_id = event["requestId"]
                target = event["request"]["url"]
                try:
                    if not target.startswith("data:"):
                        guard(self.config, target)
                        await self.transport.robots.check(target, self.transport.fetch_policy)
                        if target != url:
                            self.budget.request()
                    await session.send("Fetch.continueRequest", {"requestId": request_id})
                except SealError as exc:
                    self.errors.append(exc.code)
                    await session.send(
                        "Fetch.failRequest",
                        {"requestId": request_id, "errorReason": "BlockedByClient"},
                    )

            session.on("Fetch.requestPaused", lambda event: asyncio.ensure_future(paused(event)))
            await session.send(
                "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
            )
            response = await page.goto(
                url,
                wait_until=self.config["fetch"]["wait_until"],
                timeout=self.config["fetch"]["page_timeout_ms"],
            )
            html = await page.content()
            status = response.status if response else None
            actual = page.url
        except SealError as exc:
            return self.recorder.finish(identity, "failed", outcome="blocked", error=exc.code)
        except asyncio.CancelledError:
            self.recorder.finish(identity, "cancelled", outcome="cancelled", error="interrupted")
            raise
        except Exception:
            return self.recorder.finish(
                identity, "failed", outcome="network_failed", error="network_failed"
            )
        finally:
            await context.close()
        data = html.encode("utf-8")
        if len(data) > self.config["budget"]["response_bytes"]:
            return self.recorder.finish(
                identity, "failed", outcome="blocked", error="response_byte_budget_exceeded"
            )
        ref = self.transport.objects.put(data)
        self.transport.objects.get(ref)
        ok = status is not None and 200 <= status < 300
        return self.recorder.finish(
            identity,
            "archived",
            status=status,
            outcome="ok" if ok else "http_error",
            error=None if ok else "http_" + str(status),
            actual_url=actual,
            mime_type="text/html",
            charset="utf-8",
            representation="rendered_dom_utf8",
            byte_size=len(data),
            body_hash=ref,
            archive_ref=ref,
        )
