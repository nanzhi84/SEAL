"""Offline Discovery boundary tests; synthetic evidence, no network or PostgreSQL."""

import asyncio
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scrapy import Request
from scrapy.exceptions import IgnoreRequest
from scrapy.http import HtmlResponse
from scrapy.utils.request import RequestFingerprinter

from seal import discovery


class Connection:
    def __init__(self, rows=(), available=True, observations=0, archived_observations=0):
        self.rows = list(rows)
        self.available = available
        self.queries = []
        self.observations, self.archived_observations = observations, archived_observations
        self.attempt_epoch, self.status = 2, "running"

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        if "SELECT source_id FROM seal_run" in sql:
            return SimpleNamespace(fetchone=lambda: {"source_id": "source"})
        if "SELECT * FROM seal_source" in sql:
            return SimpleNamespace(fetchone=lambda: {"id": "source"})
        if "SELECT * FROM seal_run" in sql:
            return SimpleNamespace(
                fetchone=lambda: {
                    "id": "run",
                    "attempt_epoch": self.attempt_epoch,
                    "status": self.status,
                }
            )
        if "AS http_attempts" in sql:
            return SimpleNamespace(
                fetchone=lambda: {
                    "http_attempts": self.observations,
                    "archived_observations": self.archived_observations,
                }
            )
        return SimpleNamespace(
            fetchone=lambda: {"name": "seal_discovery" if self.available else None},
            fetchall=lambda: self.rows,
        )


def event(identity, state, reason=None, continued_by=None, status=None):
    stamp = datetime.now(timezone.utc)
    return {
        "id": identity,
        "fingerprint": identity,
        "state": state,
        "reason": reason,
        "continued_by": continued_by,
        "response_status": status,
        "requested_at": stamp,
        "archived_at": stamp if status is not None else None,
        "parsed_at": stamp if state == "parsed" else None,
        "replayed": False,
    }


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.connection = Connection()

        @contextmanager
        def connect():
            yield self.connection

        self.connect_patch = patch.object(discovery, "connect", connect)
        self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)
        self.context = {"id": "run", "source_id": "source", "attempt_epoch": 2}
        self.extension = discovery.Discovery()
        self.extension.context = self.context
        self.extension.crawler = SimpleNamespace(
            request_fingerprinter=RequestFingerprinter(), stats=Mock()
        )
        self.extension.count, self.extension.enabled, self.extension.limited = 0, True, False

    def test_multiple_parents_preserve_events_and_actual_native_fingerprint(self):
        requests = [
            Request("https://example.org/detail?b=2&a=1", meta={"seal_parent_url": parent})
            for parent in ("https://example.org/list/1", "https://example.org/list/2")
        ]
        for request in requests:
            self.extension.scheduled(request)
        self.assertNotEqual(
            requests[0].meta["seal_discovery_id"], requests[1].meta["seal_discovery_id"]
        )
        inserts = [(sql, params) for sql, params in self.connection.queries if "INSERT" in sql]
        actual = self.extension.crawler.request_fingerprinter.fingerprint(requests[0]).hex()
        self.assertEqual([params[7] for _, params in inserts], [actual, actual])
        self.assertEqual(
            [params[8] for _, params in inserts],
            ["https://example.org/list/1", "https://example.org/list/2"],
        )
        self.extension.dropped(requests[1])
        self.assertIn("reason='duplicate_request'", self.connection.queries[-1][0])

    def test_discovery_insert_locks_source_then_run_before_foreign_keys(self):
        self.extension.scheduled(Request("https://example.org/detail"))
        queries = [sql for sql, _ in self.connection.queries]
        source = next(
            index
            for index, sql in enumerate(queries)
            if "SELECT * FROM seal_source" in sql and "FOR UPDATE" in sql
        )
        run = next(
            index
            for index, sql in enumerate(queries)
            if "SELECT * FROM seal_run" in sql and "FOR UPDATE" in sql
        )
        insert = next(index for index, sql in enumerate(queries) if "INSERT" in sql)
        self.assertLess(source, run)
        self.assertLess(run, insert)

    def test_discovery_never_inserts_evidence_for_stale_or_closed_attempt(self):
        for epoch, status in ((3, "running"), (2, "complete")):
            self.connection.attempt_epoch, self.connection.status = epoch, status
            self.connection.queries.clear()
            with self.assertRaises(IgnoreRequest):
                self.extension.scheduled(Request("https://example.org/detail"))
            self.assertFalse(any("INSERT" in sql for sql, _ in self.connection.queries))

    def test_retry_gets_fresh_event_and_continuation_without_own_scheduler(self):
        request = Request("https://example.org/detail")
        self.extension.scheduled(request)
        initial = request.meta["seal_discovery_id"]
        retry = request.replace(meta={**request.meta, "retry_times": 1}, dont_filter=True)
        self.extension.scheduled(retry)
        self.assertNotEqual(initial, retry.meta["seal_discovery_id"])
        inserts = [params for sql, params in self.connection.queries if "INSERT" in sql]
        self.assertEqual(inserts[-1][11:14], (initial, "retry", True))
        self.assertIn("continued_by", self.connection.queries[-1][0])

    def test_callback_output_does_not_inherit_transport_or_parent(self):
        parent = Request("https://example.org/list", meta={"seal_discovery_id": "old"})
        response = HtmlResponse(parent.url, request=parent)
        child = Request(
            "https://example.org/detail",
            meta={
                "seal_discovery_id": "old",
                "retry_times": 1,
                "seal_parent_url": "https://old.org/",
            },
        )
        discovery.DiscoverySpiderMiddleware.lineage(response, child)
        self.extension.scheduled(child)
        insert = [params for sql, params in self.connection.queries if "INSERT" in sql][-1]
        self.assertEqual(insert[8], parent.url)
        self.assertEqual(insert[11:13], (None, "discovery"))

    def test_helper_rejection_is_captured_before_native_scheduler(self):
        request = Request(
            "https://example.org/frame", meta={"seal_helper_rejection": "iframe_out_of_scope"}
        )
        with patch.object(discovery, "record_error") as error:
            with self.assertRaises(IgnoreRequest):
                self.extension.scheduled(request)
            error.assert_called_once_with("run", 2, "iframe_out_of_scope")
        self.assertTrue(request.meta["seal_discovery_id"])
        self.assertEqual(self.connection.queries[-1][1][0], "iframe_out_of_scope")
        self.assertIn("state='skipped'", self.connection.queries[-1][0])

    def test_bound_reports_once_without_growing_events_or_error_list(self):
        self.extension.count = discovery.EVENT_LIMIT
        with patch.object(discovery, "record_error") as error:
            for _ in range(3):
                with self.assertRaises(IgnoreRequest):
                    self.extension.scheduled(Request("https://example.org/detail"))
            error.assert_called_once_with("run", 2, "discovery_budget_exceeded")
        self.assertEqual(self.connection.queries, [])

    def test_empty_callback_is_parsed_but_callback_exception_is_not(self):
        middleware = discovery.DiscoverySpiderMiddleware()
        middleware.context = self.context
        response = HtmlResponse(
            "https://example.org/detail",
            request=Request("https://example.org/detail", meta={"seal_discovery_id": "discovery"}),
        )

        async def empty():
            if False:
                yield None

        async def broken():
            raise ValueError("synthetic callback failure")
            yield None

        async def consume(result):
            return [
                output async for output in middleware.process_spider_output_async(response, result)
            ]

        with patch.object(discovery, "mark_parsed") as parsed:
            self.assertEqual(asyncio.run(consume(empty())), [])
            parsed.assert_called_once_with(self.context, response.request)
            parsed.reset_mock()
            with self.assertRaises(ValueError):
                asyncio.run(consume(broken()))
            parsed.assert_not_called()

    def test_recovered_retry_keeps_failure_evidence_without_unknown_coverage(self):
        rows = [
            event("initial", "failed", "download_failed", "retry"),
            event("retry", "parsed", status=200),
            event("duplicate", "skipped", "duplicate_request"),
        ]
        summary = discovery.summarize_discovery(Connection(rows), self.context, True)
        self.assertEqual(
            (summary["discovered"], summary["failed"], summary["deduplicated"]), (3, 1, 1)
        )
        self.assertFalse(summary["unknown_coverage"])
        self.assertIsInstance(summary["events"][0]["requested_at"], str)

    def test_transport_observations_and_callback_completion_are_distinct_counts(self):
        row = event("invalid", "failed", "invalid_record", status=200)
        row["parsed_at"] = datetime.now(timezone.utc)
        rows = [
            row,
            event("empty", "parsed", status=200),
            event("duplicate", "skipped", "duplicate_request"),
        ]
        rows[-1]["fingerprint"] = "empty"
        connection = Connection(rows, observations=2, archived_observations=2)
        summary = discovery.summarize_discovery(connection, self.context)
        self.assertEqual(summary["parsed"], 1)
        self.assertEqual(summary["callback_completed"], 2)
        self.assertEqual(summary["unique_fingerprints"], 2)
        self.assertEqual((summary["http_attempts"], summary["archived_observations"]), (2, 2))
        replay = discovery.summarize_discovery(connection, dict(self.context, mode="replay"))
        self.assertEqual((replay["http_attempts"], replay["archived_observations"]), (0, 0))

    def test_unresolved_download_and_http_failures_keep_retryable_error_codes(self):
        for row, expected in [
            (event("a", "failed", "TimeoutError"), "download_failed"),
            (event("a", "skipped", "http_error", status=503), "http_5xx"),
        ]:
            summary = discovery.summarize_discovery(Connection([row]), self.context)
            self.assertEqual(summary["unknown_coverage_reasons"], [expected])

    def test_archive_and_replay_failure_are_not_reported_as_network_failure(self):
        for reason in (
            "replay_miss",
            "replay_ambiguous",
            "sensitive_body_rejected",
            "archive_failed",
        ):
            summary = discovery.summarize_discovery(
                Connection([event("a", "failed", reason)]), self.context
            )
            self.assertEqual(summary["unknown_coverage_reasons"], [reason])

    def test_pre_upgrade_hooks_and_summary_remain_compatible(self):
        self.connection.available = False
        request = Request("https://example.org/detail", meta={"seal_discovery_id": "old"})
        discovery.mark_requested(self.context, request)
        discovery.mark_parsed(self.context, request)
        discovery.mark_inputs_failed(self.context, ["snapshot"], "invalid_record")
        self.assertTrue(all("to_regclass" in sql for sql, _ in self.connection.queries))
        summary = discovery.summarize_discovery(self.connection, self.context)
        self.assertFalse(summary["instrumented"])
        self.assertFalse(summary["unknown_coverage"])

    def test_table_install_does_not_claim_historical_discovery_was_measured(self):
        historical = dict(self.context, status="complete", report={})
        summary = discovery.summarize_discovery(self.connection, historical)
        self.assertFalse(summary["instrumented"])
        self.assertEqual(summary["unknown_coverage_reasons"], ["discovery_not_recorded"])
        measured_empty = dict(historical, report={"discovery": {"instrumented": True}})
        self.assertFalse(
            discovery.summarize_discovery(self.connection, measured_empty)["unknown_coverage"]
        )


if __name__ == "__main__":
    unittest.main()
