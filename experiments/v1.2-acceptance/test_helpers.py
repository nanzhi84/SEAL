"""Offline helper/Recipe contracts; fixtures are synthetic, never live evidence."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from attachment_fixtures import docx_bytes, xls_bytes
from scrapy import Request
from scrapy.http import HtmlResponse, JsonResponse, Response, TextResponse

from seal.core import Objects, SealError
from seal.helpers import (
    attachment_items,
    attachment_record,
    follow,
    html_record,
    json_records,
    link_requests,
    static_resources,
    table_records,
)
from seal.record_validation import located_value, validate_record

ROOT = Path(__file__).resolve().parents[2]


class HelperContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"SEAL_ARCHIVE": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def response(
        self, body, *, url="https://example.test/detail/1", role="detail", kind=HtmlResponse
    ):
        raw = body.encode() if isinstance(body, str) else body
        objects = Objects()
        snapshot = objects.put_json(
            {
                "url": url,
                "request_url": url,
                "method": "GET",
                "status": 200,
                "body_hash": objects.put(raw),
                "encoding": "utf-8" if kind != Response else None,
            }
        )
        request = Request(
            url,
            meta={
                "seal_role": role,
                "seal_snapshot_id": snapshot,
                "seal_observation_id": "observation-for-" + snapshot,
            },
        )
        kwargs = {"encoding": "utf-8"} if kind != Response else {}
        return kind(url, body=raw, request=request, **kwargs)

    def verified(self, item):
        output, _ = validate_record(item)
        self.assertEqual(output["data"], item["data"])
        return output

    def test_two_distinct_html_templates_and_exact_evidence(self):
        article = self.response("<h1>Court notice</h1><article><p>First</p><p>Second</p></article>")
        item = html_record(article)
        self.verified(item)
        self.assertEqual(item["data"], {"title": "Court notice", "body": "First Second"})
        bulletin = self.response(
            '<div class="heading"><strong>Agency decision</strong></div>'
            '<section id="fontzoom"><div>Different <em>layout</em></div></section>'
            '<span class="published">2026年10月9日</span>'
        )
        item = html_record(bulletin, title=".heading", body="#fontzoom", date=".published")
        self.verified(item)
        self.assertEqual(item["data"]["date"], "2026年10月9日")
        item["data"]["body"] = "Unlocated replacement"
        with self.assertRaisesRegex(SealError, "record_field_locator_mismatch"):
            validate_record(item)

    def test_nested_json_ten_typed_records_keys_and_reordered_evidence(self):
        rows = [
            {
                "id": i,
                "name": f"Entity {i}",
                "score": i + 0.5,
                "active": i % 2 == 0,
                "closed": None,
                "tags": ["a", i],
                "nested": {"valid": True},
                "a/b~c": "escaped",
            }
            for i in range(10)
        ]
        first = self.response(
            json.dumps({"data": {"data": {"dataList": rows}}}),
            url="https://example.test/api?pageNo=1&pageSize=10&q=FIXED",
            role="api",
            kind=JsonResponse,
        )
        items = list(json_records(first))
        self.assertEqual(len(items), 10)
        for item in items:
            self.verified(item)
            self.assertIsNone(item["detail_url"])
            self.assertEqual(item["frozen_parent_request"]["url"], first.url)
            self.assertIn("a~1b~0c", item["locators"]["a/b~c"]["pointer"])
        second = self.response(
            json.dumps({"data": {"data": {"dataList": rows[::-1]}}}),
            url=first.url,
            role="api",
            kind=JsonResponse,
        )
        again = list(json_records(second))
        for item in again:
            self.verified(item)
        self.assertEqual(
            {item["record_key"]: item["data"] for item in items},
            {item["record_key"]: item["data"] for item in again},
        )
        self.assertNotEqual(items[0]["locators"], again[-1]["locators"])

    def test_invalid_json_row_does_not_drop_other_rows_or_accept_position_keys(self):
        response = self.response(
            json.dumps(
                {
                    "data": {
                        "data": {
                            "dataList": [
                                {"id": "stable", "n": 1},
                                {"name": "missing key"},
                                {"id": True},
                                {"id": 1.5},
                                {"id": "stable-2"},
                            ]
                        }
                    }
                }
            ),
            role="api",
            kind=JsonResponse,
        )
        items = list(json_records(response))
        self.assertEqual(
            [i["record_key"] for i in items if i["type"] == "record"], ["stable", "stable-2"]
        )
        self.assertEqual(sum(i["type"] == "diagnostic" for i in items), 3)

    def test_table_cell_business_keys_and_malformed_row_isolation(self):
        response = self.response(
            "<table><tbody><tr><td>SAFE-003</td><td>Company C</td></tr>"
            "<tr><td>SAFE-001</td><td>Company A</td></tr><tr><td>bad row</td></tr></tbody></table>",
            url="https://example.test/illegal?siteid=beijing",
            role="iframe",
        )
        items = list(
            table_records(
                response,
                rows="tbody tr",
                fields={"id": "td:nth-child(1)", "name": "td:nth-child(2)"},
                key_field="id",
            )
        )
        for item in items[:2]:
            self.verified(item)
        self.assertEqual([i["record_key"] for i in items[:2]], ["SAFE-003", "SAFE-001"])
        self.assertEqual(items[-1]["type"], "diagnostic")
        self.assertIsNone(items[0]["detail_url"])

    def test_discoveries_keep_duplicate_parent_evidence_without_copying_exchange_ids(self):
        response = self.response(
            '<ul><li><a href="/detail/2">Second</a></li>'
            '<li><a href="/detail/2">Again</a></li></ul>',
            url="https://example.test/list",
            role="list",
        )
        requests = list(link_requests(response, "li a", "detail", lambda _: None))
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertFalse(request.dont_filter)
            self.assertEqual(request.meta["seal_parent_url"], response.url)
            self.assertEqual(
                request.meta["seal_parent_snapshot_id"], response.meta["seal_snapshot_id"]
            )
            self.assertNotIn("seal_snapshot_id", request.meta)
            self.assertNotIn("seal_observation_id", request.meta)
        explicit = follow(response, "/next", "list", lambda _: None)
        self.assertEqual(explicit.url, "https://example.test/next")

    def test_iframe_business_attachment_and_rejected_frame_all_remain_discoverable(self):
        response = self.response(
            '<iframe src="/static/table"></iframe><iframe src="https://outside.test/frame"></iframe>'
            '<a class="attachment" href="https://files.test/business/notice.csv">Public CSV</a>'
            '<script src="/display.js"></script><link href="/style.css">',
        )
        requests = list(
            static_resources(
                response, iframe_callback=lambda _: None, attachment_callback=lambda _: None
            )
        )
        self.assertEqual(len(requests), 3)
        self.assertEqual(
            [r.meta["seal_role"] for r in requests], ["iframe", "iframe", "attachment"]
        )
        self.assertEqual(requests[1].meta["seal_helper_rejection"], "iframe_out_of_scope")
        self.assertNotIn("seal_helper_rejection", requests[2].meta)
        parent = requests[2].cb_kwargs["parent"]
        csv = self.response(
            "name,total\nA,7\nB,9\n", url=requests[2].url, role="attachment", kind=TextResponse
        )
        item = attachment_record(csv, parent=parent)
        self.verified(item)
        self.assertEqual(item["data"]["title"], "name,total")
        self.assertEqual(
            item["supplementary_inputs"][0]["snapshot_id"], response.meta["seal_snapshot_id"]
        )
        self.assertEqual(item["data"], attachment_record(csv)["data"])

    def test_pdf_text_locator_and_damaged_attachments_retain_archive(self):
        spec = importlib.util.spec_from_file_location(
            "v11_fixture_site", ROOT / "experiments/v1.1-runtime-acceptance/e2e/site.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pdf = self.response(
            module.pdf_bytes(),
            url="https://example.test/business/notice.pdf",
            role="attachment",
            kind=Response,
        )
        item = attachment_record(pdf)
        self.verified(item)
        self.assertEqual(item["data"]["body"], "Synthetic PDF notice")
        excel = self.response(
            b"unsupported workbook bytes",
            url="https://example.test/list.xls",
            role="attachment",
            kind=Response,
        )
        with self.assertRaisesRegex(SealError, "xls_parse_failed"):
            attachment_record(excel)
        snapshot = Objects().json(excel.meta["seal_snapshot_id"])
        self.assertEqual(Objects().get(snapshot["body_hash"]), excel.body)
        broken = self.response(b"broken PDF", url="https://example.test/broken.pdf", kind=Response)
        with self.assertRaisesRegex(SealError, "pdf_parse_failed"):
            attachment_record(broken)

    # Isolated parser failure boundaries are declared before implementation:
    # malformed/empty files, merged layouts, Unicode/typed cell values, ordered
    # paragraphs/tables, unsupported embedded content, fabricated field values,
    # invalid selections, and unstable positional identities.
    def test_xls_all_sheets_effective_cells_merged_ranges_and_stable_document_key(self):
        response = self.response(
            xls_bytes(), url="https://example.test/business/public.xls", kind=Response
        )
        item = attachment_record(response)
        self.verified(item)
        self.assertEqual(item["record_key"], response.url)
        self.assertEqual(item["data"]["title"], "全国普通高等学校名单")
        sheets = item["data"]["worksheets"]
        self.assertEqual([sheet["name"] for sheet in sheets], ["公开高校", "说明"])
        self.assertEqual(sheets[0]["merged_cells"], [[0, 1, 0, 4]])
        self.assertEqual(sheets[0]["rows"][2], ["11001", "示例大学", 42.5, True])
        self.assertEqual(sheets[0]["rows"][3], ["11002", "海滨学院", 0.0, False])
        self.assertEqual(sheets[0]["rows"][4][0], "2026-10-10T00:00:00")
        self.assertEqual((len(sheets[0]["rows"]), len(sheets[0]["rows"][0])), (5, 4))
        self.assertEqual(sheets[1]["hidden"], 1)
        self.assertIn("仅使用公开合成测试数据", item["data"]["body"])
        item["data"]["worksheets"][0]["rows"][2][1] = "unlocated replacement"
        with self.assertRaisesRegex(SealError, "record_field_locator_mismatch"):
            validate_record(item)

    def test_docx_paragraph_and_merged_table_order_remains_recomputable(self):
        response = self.response(
            docx_bytes(), url="https://example.test/business/classification.docx", kind=Response
        )
        item = attachment_record(response)
        self.verified(item)
        self.assertEqual(item["record_key"], response.url)
        self.assertEqual(item["data"]["title"], "行业分类合成公开样本")
        blocks = item["data"]["blocks"]
        self.assertEqual(
            [block.get("kind", "paragraph") for block in blocks],
            ["paragraph", "table", "paragraph"],
        )
        self.assertEqual(blocks[1]["rows"][0]["cells"][0]["span"], 2)
        self.assertEqual(blocks[-1]["text"], "表格之后的解释文字")
        self.assertEqual(
            item["data"]["body"],
            "行业分类合成公开样本\n门类与代码\nA\t农业\nB\t采矿业\n表格之后的解释文字",
        )

    def test_unsupported_docx_content_and_damaged_bytes_fail_with_archived_evidence(self):
        for raw, suffix, code in (
            (docx_bytes(unsupported=True), ".docx", "docx_unsupported_content"),
            (docx_bytes(irregular_table=True), ".docx", "docx_parse_failed"),
            (b"damaged OOXML bytes", ".docx", "docx_parse_failed"),
            (b"damaged BIFF bytes", ".xls", "xls_parse_failed"),
            (b"unsupported workbook bytes", ".xlsx", "unsupported_content_type"),
        ):
            response = self.response(raw, url="https://example.test/public" + suffix, kind=Response)
            with self.subTest(code=code):
                with self.assertRaisesRegex(SealError, code):
                    attachment_record(response)
                snapshot = Objects().json(response.meta["seal_snapshot_id"])
                self.assertEqual(Objects().get(snapshot["body_hash"]), raw)

    def test_partial_docx_emits_verified_available_content_and_explicit_diagnostic(self):
        response = self.response(
            docx_bytes(unsupported=True), url="https://example.test/public.docx", kind=Response
        )
        item, error = list(attachment_items(response))
        self.verified(item)
        self.assertIn("表格之后的解释文字", item["data"]["body"])
        self.assertEqual(item["data"]["coverage"]["unparsed_elements"], {"altChunk": 1})
        self.assertEqual(error["code"], "docx_unsupported_content")
        self.assertEqual(error["snapshot_id"], item["snapshot_id"])
        item["data"]["coverage"]["unparsed_elements"] = {}
        with self.assertRaisesRegex(SealError, "record_field_locator_mismatch"):
            validate_record(item)

    def test_attachment_locator_selection_cannot_supply_unlocated_values(self):
        for kind, raw in (("xls", xls_bytes()), ("docx", docx_bytes())):
            response = self.response(raw, url="https://example.test/public." + kind, kind=Response)
            snapshot = Objects().json(response.meta["seal_snapshot_id"])
            with self.subTest(kind=kind):
                for locator in (
                    {"kind": kind, "selection": "unlocated"},
                    {"kind": kind, "selection": "title", "literal": "fake"},
                    {"kind": kind, "selection": "structure", "transform": "date_iso"},
                ):
                    with self.assertRaises(SealError):
                        located_value(snapshot, raw, locator)


if __name__ == "__main__":
    unittest.main()
