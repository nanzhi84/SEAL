"""Synthetic archive contracts; no fixture is evidence of real site coverage."""

import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from scrapy import Request
from scrapy.http import HtmlResponse

from seal.core import Objects, SealError
from seal.helpers import fallback_html_record, html_strategy_record
from seal.record_validation import located_value, validate_record

LONG = "本规定适用于公开信息采集与来源核验，应当保存原始文书及其历史版本。" * 4
LEGAL = (
    '<html><head><title>法律文书</title><meta name="publishdate" content="2026-10-09"></head>'
    '<body><nav><a href="/">栏目导航</a></nav><h1>公开决定</h1><article>'
    "<h2>第一章 总则</h2><p>第一条 依法保存公开资料。</p>"
    f"<p>{LONG}</p><h3>一、执行要求</h3>"
    '<ol start="3"><li>保存原文</li><li value="8">保存证据</li></ol>'
    '<table><tr><th colspan="2">适用范围</th></tr>'
    '<tr><td>事项</td><td rowspan="2">公开文书</td></tr><tr><td>证据</td></tr></table>'
    '<p><a href="/files/notice!.pdf?q=a%2Bb#page=2">附件一</a></p>'
    "<script>unrelated tracking payload</script></article></body></html>"
)


class FallbackContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"SEAL_ARCHIVE": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def response(self, html, *, url="https://example.test/article!1?q=a%2Bb", status=200):
        raw = html.encode("utf-8")
        objects = Objects()
        snapshot_id = objects.put_json(
            {
                "url": url,
                "method": "GET",
                "status": status,
                "encoding": "utf-8",
                "body_hash": objects.put(raw),
            }
        )
        request = Request(
            url,
            meta={
                "seal_snapshot_id": snapshot_id,
                "seal_observation_id": "observation-" + snapshot_id,
                "seal_role": "detail",
            },
        )
        return HtmlResponse(url, status=status, body=raw, request=request, encoding="utf-8")

    def verified(self, item):
        output, _ = validate_record(item)
        self.assertEqual(output["data"], item["data"])
        return item

    def test_unknown_legal_document_preserves_hierarchy_numbers_and_tables(self):
        response = self.response(LEGAL)
        item = self.verified(fallback_html_record(response))
        self.assertEqual(item["data"]["title"], "公开决定")
        self.assertEqual(item["data"]["date"], "2026-10-09")
        self.assertEqual(response.meta["seal_html_strategy"], "fallback")
        blocks = item["data"]["blocks"]
        self.assertEqual(blocks[0], {"kind": "heading", "level": 2, "text": "第一章 总则"})
        self.assertEqual(blocks[1], {"kind": "paragraph", "text": "第一条 依法保存公开资料。"})
        self.assertEqual(blocks[3], {"kind": "heading", "level": 3, "text": "一、执行要求"})
        self.assertEqual([blocks[i]["number"] for i in (4, 5)], [3, 8])
        self.assertEqual(
            blocks[6]["rows"],
            [
                [{"text": "适用范围", "header": True, "rowspan": 1, "colspan": 2}],
                [
                    {"text": "事项", "header": False, "rowspan": 1, "colspan": 1},
                    {"text": "公开文书", "header": False, "rowspan": 2, "colspan": 1},
                ],
                [{"text": "证据", "header": False, "rowspan": 1, "colspan": 1}],
            ],
        )
        self.assertIn("第一条 依法保存公开资料。\n", item["data"]["body"])
        self.assertIn("3. 保存原文\n8. 保存证据\n", item["data"]["body"])
        self.assertIn("事项\t公开文书", item["data"]["body"])
        self.assertNotIn("tracking", item["data"]["body"])
        self.assertNotIn("栏目导航", item["data"]["body"])

    def test_original_url_and_attachment_links_have_independent_evidence(self):
        item = self.verified(fallback_html_record(self.response(LEGAL)))
        self.assertEqual(item["data"]["url"], "https://example.test/article!1?q=a%2Bb")
        self.assertEqual(
            item["data"]["attachments"],
            [{"url": "https://example.test/files/notice!.pdf?q=a%2Bb", "title": "附件一"}],
        )
        for field in ("url", "attachments", "blocks", "body", "date"):
            altered = copy.deepcopy(item)
            altered["data"][field] = "fabricated replacement"
            with self.subTest(field=field):
                with self.assertRaisesRegex(SealError, "record_field_locator_mismatch"):
                    validate_record(altered)

    def test_missing_invalid_and_conflicting_dates_remain_verified_null(self):
        for metadata in (
            "",
            '<time datetime="2026-02-30"></time>',
            '<time datetime="2026-10-08"></time><time datetime="2026-10-09"></time>',
        ):
            response = self.response(
                f"<h1>Unknown template</h1>{metadata}<main><p>{LONG}</p></main>"
            )
            item = self.verified(fallback_html_record(response))
            self.assertIn("date", item["data"])
            self.assertIsNone(item["data"]["date"])
            item["data"]["date"] = "2026-10-10"
            with self.assertRaisesRegex(SealError, "record_field_locator_mismatch"):
                validate_record(item)

    def test_specific_first_matching_rule_wins_and_keeps_same_record_contract(self):
        response = self.response(
            f'<title>Fallback title</title><h1>Specific heading</h1><div id="known"><p>{LONG}</p></div><article><p>{LONG} extra fallback</p></article>'
        )
        rules = [
            {"hosts": ["outside.test"], "title": "h1", "body": "article"},
            {
                "hosts": ["example.test"],
                "path_regex": r"/article!",
                "match_css": "#known",
                "title": "h1",
                "body": "#known",
                "record_type": "specific_notice",
            },
            {"title": "title", "body": "article", "record_type": "later_rule"},
        ]
        item = self.verified(html_strategy_record(response, specific_rules=rules))
        self.assertEqual(item["record_type"], "specific_notice")
        self.assertEqual(item["data"]["title"], "Specific heading")
        self.assertEqual(item["data"]["body"], LONG)
        self.assertEqual(response.meta["seal_html_strategy"], "specific")
        self.assertIn("blocks", item["data"])
        self.assertIsNone(item["data"]["date"])

    def test_unmatched_rule_triggers_fallback(self):
        response = self.response(LEGAL)
        item = html_strategy_record(
            response, specific_rules=[{"match_css": ".unseen", "title": "h1", "body": ".unseen"}]
        )
        self.verified(item)
        self.assertEqual(response.meta["seal_html_strategy"], "fallback")

    def test_matched_broken_specific_does_not_hide_failure_with_fallback(self):
        response = self.response(LEGAL)
        with self.assertRaisesRegex(SealError, "ambiguous_or_missing_field"):
            html_strategy_record(
                response,
                specific_rules=[{"match_css": "article", "title": ".missing", "body": "article"}],
            )

    def test_navigation_login_error_and_empty_pages_never_emit_records(self):
        fixtures = [
            (
                "<title>Navigation</title><h1>栏目</h1><main>"
                + '<p><a href="/x">各类栏目导航链接</a></p>' * 50
                + "</main>",
                "fallback_non_document",
            ),
            (
                f'<title>Login</title><article><p>{LONG}</p><input type="password"></article>',
                "access_control_detected",
            ),
            (
                f"<title>Just a moment...</title><article><p>{LONG}</p></article>",
                "access_control_detected",
            ),
            (
                f"<title>Subscription required</title><article><p>{LONG}</p></article>",
                "access_control_detected",
            ),
            (f"<h1>404 Not Found</h1><article><p>{LONG}</p></article>", "fallback_error_page"),
            ("<title>Empty</title><body> </body>", "fallback_non_document"),
        ]
        for html, code in fixtures:
            with self.subTest(code=code):
                with self.assertRaisesRegex(SealError, code):
                    fallback_html_record(self.response(html))

    def test_specific_matching_cannot_bypass_access_control_rejection(self):
        response = self.response(f"<h1>Sign in</h1><article><p>{LONG}</p></article>")
        with self.assertRaisesRegex(SealError, "access_control_detected"):
            html_strategy_record(response, specific_rules=[{"title": "h1", "body": "article"}])

    def test_verbose_search_results_and_article_teasers_are_not_documents(self):
        list_page = "<title>Search results</title><h1>Search results</h1><main><ul>"
        list_page += f'<li><a href="/document">Public document</a><p>{LONG}</p></li>' * 5
        list_page += "</ul></main>"
        teaser_page = "<title>News index</title><main>"
        teaser_page += (
            f'<article><h2><a href="/document">Public document</a></h2><p>{LONG}</p></article>' * 5
        )
        teaser_page += "</main>"
        for html in (list_page, teaser_page):
            with self.assertRaisesRegex(SealError, "fallback_non_document"):
                fallback_html_record(self.response(html))

    def test_directory_cards_with_long_descriptions_are_not_documents(self):
        html = "<title>政务服务目录</title><h1>业务栏目</h1><main>"
        html += "".join(
            '<div><a href="/'
            + str(i)
            + '">公开事项'
            + str(i)
            + "</a><p>本栏目提供事项办理指引、政策信息、表格下载以及工作通知等内容，点击对应栏目查询。</p></div>"
            for i in range(8)
        )
        html += "</main>"
        for directory in (html, html.replace("<div>", "<article>").replace("</div>", "</article>")):
            with self.subTest(article_cards="<article>" in directory):
                with self.assertRaisesRegex(SealError, "fallback_non_document"):
                    fallback_html_record(self.response(directory))
                with self.assertRaisesRegex(SealError, "fallback_non_document"):
                    html_strategy_record(
                        self.response(directory),
                        specific_rules=[{"match_css": "main", "title": "h1", "body": "main"}],
                    )

    def test_specific_short_document_does_not_inherit_fallback_length_threshold(self):
        html = '<h1>简短公开通知</h1><div id="known"><p>本文依法公开发布。</p></div>'
        response = self.response(html)
        item = self.verified(
            html_strategy_record(response, specific_rules=[{"title": "h1", "body": "#known"}])
        )
        self.assertEqual(item["data"]["body"], "本文依法公开发布。")
        self.assertEqual(response.meta["seal_html_strategy"], "specific")

    def test_sso_prompt_without_password_or_login_heading_blocks_parsing(self):
        html = "<title>政务信息服务平台</title><h1>欢迎使用政务信息服务平台</h1><main><p>"
        html += "请先登录后查看此页面内容。通过统一身份认证平台验证您的身份后可以继续访问。" * 4
        html += '</p><a href="/sso/login">立即登录</a></main>'
        response = self.response(html)
        with self.assertRaisesRegex(SealError, "access_control_detected"):
            fallback_html_record(response)
        with self.assertRaisesRegex(SealError, "access_control_detected"):
            html_strategy_record(response, specific_rules=[{"title": "h1", "body": "main"}])

    def test_real_article_remains_selectable_next_to_directory_cards(self):
        cards = '<div><a href="/column">Column</a><p>Column description</p></div>' * 5
        html = f"<h1>Public decision</h1><main>{cards}<article><h2>第一章</h2><p>{LONG}</p></article></main>"
        item = self.verified(fallback_html_record(self.response(html)))
        self.assertEqual(item["data"]["title"], "Public decision")
        self.assertEqual(item["data"]["body"], "第一章\n" + LONG)
        self.assertNotIn("Column", item["data"]["body"])

    def test_explicit_linebreaks_remain_and_hidden_comments_are_excluded(self):
        html = f"<h1>Public notice</h1><article><p>第一条<br>第二款</p><p>{LONG}</p><!-- hidden code --></article>"
        item = self.verified(fallback_html_record(self.response(html)))
        self.assertEqual(item["data"]["blocks"][0]["text"], "第一条\n第二款")
        self.assertNotIn("hidden code", item["data"]["body"])

    def test_invalid_attachment_urls_do_not_invent_or_drop_document_content(self):
        html = f'<h1>Public notice</h1><article><p>{LONG}</p><a href="http://[bad" download>Malformed link</a><a href="javascript:download()" download>JavaScript</a></article>'
        item = self.verified(fallback_html_record(self.response(html)))
        self.assertEqual(item["data"]["attachments"], [])

    def test_archived_response_restoration_is_deterministic(self):
        original = self.response(LEGAL)
        first = self.verified(html_strategy_record(original))
        snapshot = Objects().json(first["snapshot_id"])
        replay = HtmlResponse(
            snapshot["url"],
            body=Objects().get(snapshot["body_hash"]),
            request=original.request.replace(
                meta={
                    key: value
                    for key, value in original.meta.items()
                    if key != "seal_html_strategy"
                }
            ),
            encoding=snapshot["encoding"],
        )
        with patch("socket.socket", side_effect=AssertionError("offline parsing used network")):
            second = self.verified(html_strategy_record(replay))
        self.assertEqual(first, second)

    def test_locator_cannot_accept_literals_transforms_or_multi_element_paths(self):
        item = fallback_html_record(self.response(LEGAL))
        snapshot = Objects().json(item["snapshot_id"])
        raw = Objects().get(snapshot["body_hash"])
        locators = [
            {"kind": "html_date", "literal": "2026-10-10"},
            {"kind": "response_url", "transform": "made_up"},
            {"kind": "html_blocks", "path": "//article", "selection": "unknown"},
            {"kind": "html_links", "path": "//p"},
        ]
        for locator in locators:
            with self.subTest(locator=locator):
                with self.assertRaises(SealError):
                    located_value(snapshot, raw, locator)


if __name__ == "__main__":
    unittest.main()
