"""Final representation classification preserves extensionless-download parentage."""

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from scrapy import Request
from scrapy.http import HtmlResponse, Response, TextResponse

from seal.core import SealError
from seal.helpers import attachment_record, is_attachment_response
from seal.seeded import html_links

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "seal_seeded_dynamic_test", ROOT / "recipes/seeded/recipe.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PDF_SPEC = importlib.util.spec_from_file_location(
    "seal_dynamic_pdf_fixture", ROOT / "experiments/v1.1-runtime-acceptance/e2e/site.py"
)
PDF_MODULE = importlib.util.module_from_spec(PDF_SPEC)
PDF_SPEC.loader.exec_module(PDF_MODULE)
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class DynamicAttachmentTests(unittest.TestCase):
    def spider(self, mode="collect"):
        context = {
            "mode": mode,
            "seeds": [],
            "config": {
                "robots": True,
                "output_schema": "record.v1",
                "allowed_hosts": ["example.org"],
                "allowed_path_prefixes": ["/"],
                "host_path_scopes": {},
            },
        }
        spider = MODULE.SeededSpider({"discover_sitemaps": False}, context)
        spider.crawler = SimpleNamespace(stats=Mock())
        return spider

    def parent(self):
        request = Request(
            "https://example.org/notice",
            meta={"seal_snapshot_id": "parent", "seal_observation_id": "parent-observation"},
        )
        return HtmlResponse(
            request.url,
            request=request,
            body=b'<html><a href="/download?id=123">Public report</a></html>',
            encoding="utf8",
        )

    def downloaded(self, content_type, body, kind=Response):
        parent, spider = self.parent(), self.spider()
        url, role, method = next(html_links(parent))
        self.assertEqual(role, "detail")
        request = spider.request(url, role, method, parent=parent)
        request.meta.update(seal_snapshot_id="download", seal_observation_id="download-observation")
        kwargs = {"encoding": "utf8"} if issubclass(kind, TextResponse) else {}
        response = kind(
            url,
            body=body,
            headers={"Content-Type": content_type},
            request=request,
            **kwargs,
        )
        return spider, response

    def test_unclassified_child_retains_parent_for_later_content_type(self):
        spider, response = self.downloaded("application/pdf", PDF_MODULE.pdf_bytes())
        self.assertEqual(
            response.request.cb_kwargs["parent"],
            {"snapshot_id": "parent", "observation_id": "parent-observation"},
        )
        self.assertNotIn(
            "seal_snapshot_id", spider.request("https://example.org/x", "detail", "html").meta
        )

    def test_dynamic_pdf_uses_pdf_locators_and_parent_input(self):
        spider, response = self.downloaded("application/pdf", PDF_MODULE.pdf_bytes())
        item = next(spider.parse(response, **response.request.cb_kwargs))
        self.assertEqual(item["record_type"], "document_attachment")
        self.assertEqual(item["data"]["body"], "Synthetic PDF notice")
        self.assertEqual(item["locators"]["body"]["kind"], "segments")
        self.assertEqual(item["locators"]["body"]["segments"][0]["kind"], "pdf")
        self.assertEqual(item["supplementary_inputs"], [response.request.cb_kwargs["parent"]])

    def test_dynamic_text_uses_text_locators_and_parent_input(self):
        spider, response = self.downloaded(
            "text/plain", b"Public text report\nVerified source text.", TextResponse
        )
        item = next(spider.parse(response, **response.request.cb_kwargs))
        self.assertEqual(item["data"]["title"], "Public text report")
        self.assertEqual(item["locators"]["body"]["kind"], "text")
        self.assertEqual(item["supplementary_inputs"], [response.request.cb_kwargs["parent"]])

    def test_xlsx_mime_is_recognized_but_not_claimed_as_parsed(self):
        spider, response = self.downloaded(XLSX_MIME, b"PK\x03\x04unsupported XLSX bytes")
        self.assertTrue(is_attachment_response(response))
        items = list(spider.parse(response, **response.request.cb_kwargs))
        self.assertEqual(
            items,
            [
                {
                    "type": "diagnostic",
                    "code": "unsupported_content_type",
                    "snapshot_id": "download",
                    "observation_id": "download-observation",
                }
            ],
        )

    def test_xlsx_extension_has_same_explicit_support_boundary(self):
        request = Request("https://example.org/report.xlsx")
        response = Response(request.url, request=request, body=b"PK\x03\x04XLSX")
        self.assertTrue(is_attachment_response(response))
        with self.assertRaisesRegex(SealError, "unsupported_content_type"):
            attachment_record(response)

    def test_dynamic_cross_host_requires_explicit_attachment_scope(self):
        spider, response = self.downloaded("application/pdf", PDF_MODULE.pdf_bytes())
        spider.context["config"]["allowed_hosts"].append("downloads.example.org")
        response = response.replace(url="https://downloads.example.org/download?id=123")
        items = list(spider.parse(response, **response.request.cb_kwargs))
        self.assertEqual([item.get("code") for item in items], ["attachment_host_path_required"])

    def test_dynamic_cross_host_with_explicit_scope_keeps_parent(self):
        spider, response = self.downloaded("application/pdf", PDF_MODULE.pdf_bytes())
        spider.context["config"]["allowed_hosts"].append("downloads.example.org")
        spider.context["config"]["host_path_scopes"] = {
            "example.org": ["/"],
            "downloads.example.org": ["/download"],
        }
        response = response.replace(url="https://downloads.example.org/download?id=123")
        item = next(spider.parse(response, **response.request.cb_kwargs))
        self.assertEqual(item["record_type"], "document_attachment")
        self.assertEqual(item["supplementary_inputs"], [response.request.cb_kwargs["parent"]])

    def test_recheck_direct_attachment_does_not_require_or_fetch_parent(self):
        for mime, body, kind in (
            ("application/pdf", PDF_MODULE.pdf_bytes(), Response),
            ("text/plain", b"Direct frozen attachment\nPublic original content.", TextResponse),
        ):
            with self.subTest(mime=mime):
                spider = self.spider(mode="recheck")
                request = spider.request("https://example.org/download?id=123", "detail", "seed")
                request.meta.update(seal_snapshot_id="download", seal_observation_id="observation")
                kwargs = {"encoding": "utf8"} if issubclass(kind, TextResponse) else {}
                response = kind(
                    request.url,
                    request=request,
                    headers={"Content-Type": mime},
                    body=body,
                    **kwargs,
                )
                items = list(spider.parse(response, **request.cb_kwargs))
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["record_type"], "document_attachment")
                self.assertEqual(items[0]["supplementary_inputs"], [])

    def test_final_attachment_path_is_rechecked_before_record_promotion(self):
        spider, response = self.downloaded("application/pdf", PDF_MODULE.pdf_bytes())
        spider.context["config"]["allowed_path_prefixes"] = ["/authorized"]
        items = list(spider.parse(response, **response.request.cb_kwargs))
        self.assertEqual([item.get("code") for item in items], ["request_out_of_scope"])


if __name__ == "__main__":
    unittest.main()
