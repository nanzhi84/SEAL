"""Offline snapshot query contracts; PostgreSQL exercises run separately."""

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from seal.core import Objects, SealError
from seal.inspect import inspect_record


class PersistenceContracts(unittest.TestCase):
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
