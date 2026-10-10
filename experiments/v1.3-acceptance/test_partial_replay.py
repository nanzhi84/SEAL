"""Replay accepts archived inputs and exact frozen failures, never unknown URLs."""

import unittest
from unittest.mock import AsyncMock, patch

from scrapy import Request
from scrapy.exceptions import IgnoreRequest
from scrapy.http import HtmlResponse

from seal.archive import ReplayMiddleware, request_key
from seal.discovery import ResourceFingerprinter


class PartialReplayTests(unittest.IsolatedAsyncioTestCase):
    def request(self, **kwargs):
        return Request(
            "https://example.org/document?q=a+b",
            meta={"seal_role": "detail", "seal_parent_snapshot_id": "original-parent"},
            **kwargs,
        )

    def middleware(self, request, *, reasons=("request_budget_exceeded",), inputs=()):
        middleware = ReplayMiddleware()
        middleware.context = {
            "replay_inputs": list(inputs),
            "replay_origin": {
                "contract": 1,
                "rejected_candidates": [
                    {
                        "fingerprint": ResourceFingerprinter().fingerprint(request).hex(),
                        "role": "detail",
                        "parent_snapshot_id": "original-parent",
                        "reason": reason,
                    }
                    for reason in reasons
                ],
            },
        }
        middleware.fail = AsyncMock()
        return middleware

    async def assert_failure(self, middleware, request, reason):
        with patch("seal.archive.mark_failed") as failed:
            with self.assertRaisesRegex(IgnoreRequest, "^" + reason + "$"):
                await middleware.process_request(request)
            failed.assert_called_once_with(middleware.context, request, reason)
        middleware.fail.assert_awaited_once_with(reason)

    async def test_exact_original_budget_failure_stays_budget_failure(self):
        request = self.request()
        await self.assert_failure(self.middleware(request), request, "request_budget_exceeded")

    async def test_new_unknown_url_remains_strict_miss(self):
        request = self.request()
        await self.assert_failure(
            self.middleware(request), request.replace(url="https://example.org/new"), "replay_miss"
        )

    async def test_same_url_new_parent_snapshot_remains_strict_miss(self):
        request = self.request()
        altered = request.replace(meta={**request.meta, "seal_parent_snapshot_id": "new-parent"})
        await self.assert_failure(self.middleware(request), altered, "replay_miss")

    async def test_same_url_new_role_remains_strict_miss(self):
        request = self.request()
        altered = request.replace(meta={**request.meta, "seal_role": "attachment"})
        await self.assert_failure(self.middleware(request), altered, "replay_miss")

    async def test_same_url_new_representation_headers_remain_strict_miss(self):
        request = self.request()
        altered = request.replace(headers={"Accept": "application/json"})
        await self.assert_failure(self.middleware(request), altered, "replay_miss")

    async def test_ambiguous_negative_evidence_cannot_hide_a_miss(self):
        request = self.request()
        await self.assert_failure(
            self.middleware(request, reasons=("request_budget_exceeded", "download_failed")),
            request,
            "replay_miss",
        )

    async def test_archived_input_takes_precedence_over_historical_failure(self):
        request = self.request()
        entry = {
            "key": request_key(request),
            "snapshot_id": "snapshot",
            "observation_id": "observation",
            "fetched_at": "2026-10-10T00:00:00+00:00",
        }
        middleware = self.middleware(request, inputs=[entry])
        import asyncio

        middleware.io = asyncio.Semaphore(1)
        response = HtmlResponse(request.url, request=request, body=b"<html></html>")
        with (
            patch("seal.archive.mark_requested") as requested,
            patch("seal.archive.mark_archived") as archived,
            patch("seal.archive.restore", return_value=response),
        ):
            self.assertIs(await middleware.process_request(request), response)
            requested.assert_called_once_with(middleware.context, request, replayed=True)
            archived.assert_called_once_with(
                middleware.context, request, "snapshot", "observation", 200
            )
        middleware.fail.assert_not_awaited()

    async def test_ambiguous_archived_inputs_remain_strict_error(self):
        request = self.request()
        inputs = [
            {"key": request_key(request), "snapshot_id": identity}
            for identity in ("snapshot-a", "snapshot-b")
        ]
        await self.assert_failure(
            self.middleware(request, inputs=inputs), request, "replay_ambiguous"
        )

    async def test_missing_mapping_without_original_evidence_remains_strict_miss(self):
        request = self.request()
        middleware = self.middleware(request)
        middleware.context.pop("replay_origin")
        await self.assert_failure(middleware, request, "replay_miss")


if __name__ == "__main__":
    unittest.main()
