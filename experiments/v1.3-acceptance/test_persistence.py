"""Offline read-only and termination contracts; PostgreSQL exercises run separately."""

import copy
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from seal.core import Objects, SealError
from seal.inspect import inspect_record
from seal.runs import run_termination


class PersistenceContracts(unittest.TestCase):
    def test_running_errors_are_not_a_terminal_result(self):
        run = {"status": "running", "errors": ["http_5xx"], "report": {}}
        self.assertEqual(
            run_termination(run),
            {"terminal": False, "termination": None, "terminal_reason": None},
        )

    def test_termination_preserves_frozen_evidence(self):
        cases = [
            ("complete", [], "queue_exhausted", "queue_exhausted", True),
            (
                "partial",
                ["request_budget_exceeded"],
                "budget_exhausted",
                "request_budget_exceeded",
                True,
            ),
            ("partial", ["robots_denied", "unresolved_seed"], "blocked", "robots_denied", True),
            ("retryable", ["http_5xx"], "retryable", "http_5xx", False),
            ("superseded", ["stale_attempt"], "superseded", "superseded", True),
        ]
        for status, errors, category, reason, terminal in cases:
            with self.subTest(status=status, errors=errors):
                run = {"status": status, "errors": errors, "report": {"finish_reason": "finished"}}
                before = copy.deepcopy(run)
                self.assertEqual(
                    run_termination(run),
                    {"terminal": terminal, "termination": category, "terminal_reason": reason},
                )
                self.assertEqual(run, before)

    def test_time_budget_stop_is_explicit(self):
        run = {
            "status": "partial",
            "errors": ["incomplete_crawl"],
            "report": {"finish_reason": "closespider_timeout"},
        }
        self.assertEqual(
            run_termination(run),
            {
                "terminal": True,
                "termination": "budget_exhausted",
                "terminal_reason": "closespider_timeout",
            },
        )

    def test_snapshot_query_checks_body_without_returning_it(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"SEAL_ARCHIVE": directory}),
            patch("seal.inspect.connect") as connect,
        ):
            connection = MagicMock()
            connection.execute.return_value.fetchall.return_value = []
            connect.return_value.__enter__.return_value = connection
            objects = Objects()
            body = b"Synthetic private raw response"
            key = objects.put(body)
            identity = objects.put_json(
                {
                    "contract": 1,
                    "body_hash": key,
                    "body_size": len(body),
                    "method": "GET",
                    "url": "https://example.test/notice",
                    "status": 200,
                }
            )
            value = inspect_record("snapshot", identity)
            self.assertEqual(value["archive"], {"availability": "available"})
            self.assertNotIn("body", value)
            objects.path(key).write_bytes(b"corrupted bytes")
            value = inspect_record("snapshot", identity)
            self.assertEqual(
                value["archive"], {"availability": "unavailable", "reason": "archive_corrupt"}
            )

    def test_snapshot_query_rejects_other_archived_json_objects(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"SEAL_ARCHIVE": directory}),
            patch("seal.inspect.connect"),
        ):
            identity = Objects().put_json({"contract": "seal.run_manifest.v1"})
            with self.assertRaises(SealError) as error:
                inspect_record("snapshot", identity)
            self.assertEqual(error.exception.code, "unsupported_snapshot_contract")


if __name__ == "__main__":
    unittest.main()
