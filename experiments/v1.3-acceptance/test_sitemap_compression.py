"""Bounded gzip sitemap parsing; raw archived representation stays untouched."""

import gzip
import unittest

from scrapy import Request
from scrapy.http import Response

from seal.core import SealError
from seal.seeded import is_sitemap_response, sitemap_links

URL = "https://example.org/document!open?q=a+b"
XML = (
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    f"<url><loc>{URL}</loc></url></urlset>"
).encode()


class SitemapCompressionTests(unittest.TestCase):
    def response(self, body, *, url="https://example.org/sitemap.xml.gz", headers=None, role=None):
        request = Request(url, meta={"seal_role": role} if role else {})
        return Response(url, body=body, headers=headers, request=request)

    def assert_failure(self, body, code, **kwargs):
        response = self.response(body)
        with self.assertRaises(SealError) as caught:
            list(sitemap_links(response, **kwargs))
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(response.body, body)

    def test_plain_xml_index_and_urlset(self):
        for body, role in (
            (XML, "detail"),
            (
                XML.replace(b"urlset", b"sitemapindex")
                .replace(b"<url>", b"<sitemap>")
                .replace(b"</url>", b"</sitemap>"),
                "sitemap",
            ),
        ):
            with self.subTest(role=role):
                response = self.response(body, url="https://example.org/sitemap.xml")
                self.assertEqual(list(sitemap_links(response)), [(URL, role)])

    def test_gzip_file_without_http_content_encoding_preserves_raw_bytes(self):
        compressed = gzip.compress(XML, mtime=0)
        response = self.response(compressed, headers={"Content-Type": "application/gzip"})
        self.assertEqual(list(sitemap_links(response)), [(URL, "detail")])
        self.assertEqual(response.body, compressed)
        self.assertNotIn("Content-Encoding", response.headers)

    def test_gzip_magic_detected_without_filename_or_content_encoding(self):
        response = self.response(
            gzip.compress(XML), url="https://example.org/map?id=7", role="sitemap"
        )
        self.assertTrue(is_sitemap_response(response))
        self.assertEqual(list(sitemap_links(response)), [(URL, "detail")])

    def test_already_http_decoded_gzip_xml_is_not_decompressed_again(self):
        response = self.response(XML, headers={"Content-Encoding": "gzip"})
        self.assertEqual(list(sitemap_links(response, max_size=len(XML))), [(URL, "detail")])
        self.assertEqual(response.body, XML)

    def test_gzip_filename_with_query_is_identified(self):
        response = self.response(
            gzip.compress(XML), url="https://example.org/MAP.XML.GZ?download=1"
        )
        self.assertTrue(is_sitemap_response(response))

    def test_gzip_exact_decompression_limit_is_accepted(self):
        response = self.response(gzip.compress(XML))
        self.assertEqual(list(sitemap_links(response, max_size=len(XML))), [(URL, "detail")])

    def test_gzip_expansion_is_bounded_before_any_candidate_is_yielded(self):
        compressed = gzip.compress(XML + b" " * 200000, mtime=0)
        self.assertLess(len(compressed), 1024)
        self.assert_failure(compressed, "sitemap_size_exceeded", max_size=1024)

    def test_plain_xml_obeys_the_same_size_limit(self):
        self.assert_failure(XML, "sitemap_size_exceeded", max_size=len(XML) - 1)

    def test_corrupt_gzip_header_is_rejected(self):
        compressed = bytearray(gzip.compress(XML))
        compressed[2] = 7
        self.assert_failure(bytes(compressed), "sitemap_decompression_failed")

    def test_corrupt_gzip_checksum_is_not_silently_tolerated(self):
        compressed = bytearray(gzip.compress(XML))
        compressed[-8] ^= 128
        self.assert_failure(bytes(compressed), "sitemap_decompression_failed")

    def test_truncated_gzip_footer_is_not_silently_tolerated(self):
        self.assert_failure(gzip.compress(XML)[:-4], "sitemap_decompression_failed")

    def test_concatenated_gzip_members_are_checked_and_bounded_together(self):
        midpoint = len(XML) // 2
        body = gzip.compress(XML[:midpoint]) + gzip.compress(XML[midpoint:])
        self.assertEqual(list(sitemap_links(self.response(body))), [(URL, "detail")])
        self.assert_failure(body, "sitemap_size_exceeded", max_size=len(XML) - 1)
        self.assert_failure(body[:-4], "sitemap_decompression_failed")

    def test_empty_gzip_has_explicit_xml_parse_failure(self):
        self.assert_failure(gzip.compress(b""), "sitemap_parse_failed")

    def test_limit_cannot_disable_decompression_bound(self):
        for limit in (0, -1, True, None):
            with self.subTest(limit=limit):
                self.assert_failure(
                    gzip.compress(XML), "sitemap_size_limit_invalid", max_size=limit
                )


if __name__ == "__main__":
    unittest.main()
