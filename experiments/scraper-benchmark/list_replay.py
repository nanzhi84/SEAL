#!/usr/bin/env python3
"""Discover -> paginate -> fetch five details over local HTTP archived real HTML.

Only list page 1 is a seed. Original links are mapped to allowlisted loopback
paths, so no website is contacted and no extra document can escape the cap.
This tests workflow wiring; it is explicitly NOT a new live crawl.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import scrapy
from scrapy.crawler import CrawlerProcess

from bench import recipes
from bench.common import RAW, RESULTS, read_json, sha256_hex, write_json
from bench.targets import BY_ID

ORIGIN = 'https://www.court.gov.cn'
PAGE1 = BY_ID['A_list_p1']['url']
PAGE2 = read_json(RESULTS / 'parsed/A_list_p1.raw.json')['next_page']
PAGES = {PAGE1: 'A_list_p1', PAGE2: 'A_list_p1__p2'}
ARCHIVE = {**PAGES, **{BY_ID[f'A_detail_{i}']['url']: f'A_detail_{i}' for i in range(1, 6)}}
OUT = {'sample_class': 'REAL_ARCHIVE_LOCAL_HTTP_REPLAY', 'list_count': 0,
       'details': [], 'edges': [], 'requests': [], 'external_requests': 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        url = ORIGIN + self.path
        tid = ARCHIVE.get(url)
        status = 200 if tid else 404
        body = (RAW / f'{tid}.raw.html').read_bytes() if tid else b'not in archive'
        OUT['requests'].append({'url': url, 'status': status, 'at_monotonic': time.monotonic()})
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Walk(scrapy.Spider):
    name = 'archived_court_walk'

    def request(self, original, callback, parent=None):
        if original not in ARCHIVE:
            raise ValueError('outside archived allowlist')
        p = urlparse(original)
        return scrapy.Request(self.base + p.path + ('?' + p.query if p.query else ''),
                              callback=callback, errback=self.failed,
                              meta={'original': original, 'parent': parent, 'proxy': None})

    async def start(self):
        yield self.request(PAGE1, self.parse_list)

    def parse_list(self, response):
        url = response.meta['original']
        doc = recipes.court_list(response.selector, url, sha256_hex(response.body))
        OUT['list_count'] += 1
        if url == PAGE1:
            if doc['next_page']:
                yield self.request(doc['next_page'], self.parse_list)
            for link in doc['document_links'][:5]:
                yield self.request(link['url'], self.parse_detail, parent=url)

    def parse_detail(self, response):
        url = response.meta['original']
        doc = recipes.court_detail(response.selector, url, sha256_hex(response.body))
        original = read_json(RESULTS / 'parsed' / (ARCHIVE[url] + '.raw.json'))
        OUT['details'].append({'source_url': url, 'title': doc['title'],
                               'body_chars': len(doc['body_text']),
                               'matches_archived': all(doc[f] == original[f]
                                                       for f in ['title', 'published_at', 'body_text'])})
        OUT['edges'].append({'parent_url': response.meta['parent'], 'detail_url': url})

    def failed(self, failure):
        OUT.setdefault('failures', []).append({'url': failure.request.meta['original'],
                                               'error_type': failure.type.__name__})


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    start = time.perf_counter()
    proc = CrawlerProcess({'CONCURRENT_REQUESTS': 1, 'CONCURRENT_REQUESTS_PER_DOMAIN': 1,
                          'DOWNLOAD_DELAY': 3, 'DOWNLOAD_DELAY_JITTER': 0,
                          'RETRY_ENABLED': False, 'REDIRECT_ENABLED': False,
                          'ROBOTSTXT_OBEY': False, 'HTTPPROXY_ENABLED': False,
                          'TELNETCONSOLE_ENABLED': False, 'REMOTE_CONTROL_ENABLED': False,
                          'LOG_LEVEL': 'WARNING', 'DOWNLOAD_TIMEOUT': 10})
    try:
        proc.crawl(Walk, base=f'http://127.0.0.1:{server.server_port}')
        proc.start()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    OUT.update({'request_count': len(OUT['requests']), 'elapsed_s': time.perf_counter() - start,
                'all_bodies_match_archived': len(OUT['details']) == 5 and
                                            all(d['matches_archived'] for d in OUT['details'])})
    write_json(RESULTS / 'list_replay_e2e.json', OUT)
    print(json.dumps(OUT, ensure_ascii=False, indent=2))
    return int(bool(OUT.get('failures')) or not OUT['all_bodies_match_archived'])


if __name__ == '__main__':
    raise SystemExit(main())
