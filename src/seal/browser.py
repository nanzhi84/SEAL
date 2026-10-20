"""Rendered DOM acquisition; all browser HTTP inputs use the controlled transport.

Raw main responses, redirects and subresources have independent HTTP Acquisition facts.
The rendered UTF-8 DOM is an additional representation, not an extra network request.
"""

import asyncio
import hashlib

from .core import SealError
from .scope import guard
from .transport import REDIRECTS, request_key


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
        self.budget.active()
        key = request_key(request)
        identity = self.recorder.start(
            key, "rendered_dom", "content", "GET", url, {"role": request.get("role", "page")}
        )
        context = await self.browser.new_context(
            user_agent=self.config["user_agent"],
            accept_downloads=False,
            ignore_https_errors=False,
            service_workers="block",
        )
        errors, facts, pending = [], {}, set()

        async def route_input(route):
            current = asyncio.current_task()
            pending.add(current)
            browser_request = route.request
            try:
                target = guard(self.config, browser_request.url)
                await self.transport.robots.check(target, self.transport.fetch_policy)
                body = browser_request.post_data_buffer
                raw_key = request_key(
                    {
                        "url": target,
                        "method": browser_request.method,
                        "role": "browser_http",
                        "form": {"body_hash": hashlib.sha256(body).hexdigest()} if body else None,
                    }
                )
                previous = facts.get(browser_request.redirected_from)
                row = await self.transport.exchange(
                    target,
                    browser_request.method,
                    body,
                    raw_key,
                    "content",
                    {
                        "rendered_acquisition_id": identity,
                        "resource_type": browser_request.resource_type,
                    },
                    previous,
                    "redirect" if previous else None,
                    headers={
                        name: value
                        for name, value in (await browser_request.all_headers()).items()
                        if name.lower()
                        not in {"accept-encoding", "host", "connection", "content-length"}
                    },
                )
                facts[browser_request] = row["id"]
                if row["state"] != "archived":
                    raise SealError(row["error"] or "network_failed")
                if row["outcome"] not in {"ok", "redirect"}:
                    raise SealError(row["error"] or "network_failed")
                # Transport archives decoded HTTP entity bytes. Chromium must not decode
                # them again; preserve semantic headers, including redirects and cookies.
                headers = {
                    name: value
                    for name, value in row["headers"].items()
                    if name.lower()
                    not in {"content-encoding", "content-length", "transfer-encoding", "connection"}
                }
                await route.fulfill(
                    status=row["http_status"],
                    headers=headers,
                    body=self.transport.objects.get(row["archive_ref"]),
                )
            except SealError as exc:
                errors.append(exc.code)
                await route.abort("blockedbyclient")
            except asyncio.CancelledError:
                errors.append("interrupted")
                raise
            except Exception:
                errors.append("network_failed")
                await route.abort("failed")
            finally:
                pending.discard(current)

        try:
            await context.route("**/*", route_input)
            page = await context.new_page()
            response = await page.goto(
                url,
                wait_until=self.config["fetch"]["wait_until"],
                timeout=self.config["fetch"]["page_timeout_ms"],
            )
            while pending:
                self.budget.active()
                await asyncio.gather(*tuple(pending))
            self.budget.active()
            if errors:
                raise SealError(errors[0])
            html = await page.content()
            status = response.status if response else None
            actual = page.url
        except asyncio.CancelledError:
            self.recorder.finish(identity, "cancelled", outcome="cancelled", error="interrupted")
            raise
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, SealError)
                else (errors[0] if errors else "network_failed")
            )
            if code in {"stale_attempt", "inactive_attempt"}:
                raise SealError(code) from exc
            state = "cancelled" if code == "interrupted" else "failed"
            return self.recorder.finish(identity, state, outcome="blocked", error=code)
        finally:
            await context.close()
        data = html.encode("utf-8")
        if len(data) > self.config["budget"]["response_bytes"]:
            return self.recorder.finish(
                identity, "failed", outcome="blocked", error="response_byte_budget_exceeded"
            )
        self.budget.active()
        ref = self.transport.objects.put(data)
        self.transport.objects.get(ref)
        self.budget.active()
        ok = status is not None and 200 <= status < 300 and status not in REDIRECTS
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
