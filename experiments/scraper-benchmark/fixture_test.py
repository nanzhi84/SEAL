#!/usr/bin/env python3
"""Real loopback HTTP -> Scrapy / scrapy-playwright -> JSON artifacts.

All inputs here are SYNTHETIC, not performance/quality scores of live websites.
Playwright only renders an unrestricted local JS + iframe fixture. It never
retries F/G. No external network, proxies, screenshots of third-party pages.
"""
import asyncio
import difflib
import json
import resource
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy_playwright.page import PageMethod

from bench import recipes
from bench.common import FIXTURES, RESULTS, now_iso, sha256_hex, write_json
from bench.extract import extract_generic
from bench.revision import content_hash, observation

DYNAMIC = b'''<!doctype html><meta charset="utf-8"><title>Local JS fixture</title>
<main id="data"></main><iframe src="/frame.html"></iframe><script>
setTimeout(()=>{document.querySelector('#data').textContent='JS_RECORD_001';},100);
</script>'''
FRAME = b'<!doctype html><meta charset="utf-8"><p>IFRAME_RECORD_002</p>'
REQUESTS = []
BROWSER_REQUESTS = []
BLOCKED_EXTERNAL = []
BROWSER_LOCK = None
LAST_BROWSER_END = 0
OUT = {'sample_class': 'SYNTHETIC', 'versions': {}, 'external_requests': 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        content = {
            '/dynamic.html': (DYNAMIC, 'text/html'),
            '/frame.html': (FRAME, 'text/html'),
        }.get(path)
        if path in ['/version_a.html', '/version_b.html', '/version_c.html',
                    '/sample.json', '/sample.txt']:
            content = ((FIXTURES / path[1:]).read_bytes(),
                       'application/json' if path.endswith('.json') else
                       'text/plain' if path.endswith('.txt') else 'text/html')
        body, mime = content or (b'not found', 'text/plain')
        status = 200 if content else 404
        REQUESTS.append({'path': path, 'at': now_iso(), 'monotonic': time.monotonic(),
                         'bytes': len(body), 'status': status})
        self.send_response(status)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def route_local(route):
    """Meter every browser subresource; abort external egress before dispatch."""
    global BROWSER_LOCK, LAST_BROWSER_END
    url = route.request.url
    if urlparse(url).hostname != '127.0.0.1':
        BLOCKED_EXTERNAL.append({'url_scheme': urlparse(url).scheme})
        await route.abort()
        return
    if BROWSER_LOCK is None:
        BROWSER_LOCK = asyncio.Lock()
    async with BROWSER_LOCK:
        await asyncio.sleep(max(0, 3 - (time.monotonic() - LAST_BROWSER_END)))
        BROWSER_REQUESTS.append({'path': urlparse(url).path, 'at': now_iso()})
        response = await route.fetch(timeout=10000, max_redirects=0)
        await route.fulfill(response=response)
        LAST_BROWSER_END = time.monotonic()


async def init_page(page, request):
    # Page routes take precedence over scrapy-playwright's route and context routes.
    await page.route('**/*', route_local)


async def capture_frames(page):
    return [{'url_path': urlparse(frame.url).path,
             'text': await frame.locator('body').inner_text()} for frame in page.frames]


class FixtureSpider(scrapy.Spider):
    name = 'synthetic_fixture'

    async def start(self):
        if self.mode == 'playwright':
            yield scrapy.Request(self.base + '/dynamic.html', callback=self.parse_dynamic,
                                 errback=self.failed, meta={
                'proxy': None, 'playwright': True,
                'playwright_page_init_callback': init_page,
                'playwright_page_methods': [
                    PageMethod('wait_for_selector', '#data:text("JS_RECORD_001")'),
                    PageMethod(capture_frames)],
            })
            return
        for name in ['a', 'b', 'c', 'a_repeat']:
            path = '/version_' + ('a' if name == 'a_repeat' else name) + '.html'
            yield scrapy.Request(self.base + path, callback=self.parse_version,
                                 errback=self.failed, dont_filter=True,
                                 meta={'proxy': None, 'version': name})
        for path, callback in [('/sample.json', self.parse_json),
                               ('/sample.txt', self.parse_txt),
                               ('/dynamic.html', self.parse_dynamic)]:
            yield scrapy.Request(self.base + path, callback=callback,
                                 errback=self.failed, meta={'proxy': None})

    def parse_version(self, response):
        name = response.meta['version']
        # All fixture variations represent the same document identity.
        source = 'http://fixture.invalid/document/999'
        snapshot = sha256_hex(response.body)
        doc = recipes.court_detail(response.selector, source, snapshot)
        record = observation(response.body, doc, source, len(OUT['versions']) + 1)
        record['generic_trafilatura'] = extract_generic(response.text, source)
        OUT['versions'][name] = record
        path = RESULTS / 'raw' / 'fixtures'
        path.mkdir(exist_ok=True)
        (path / f'{name}.html').write_bytes(response.body)

    def parse_json(self, response):
        data = response.json()
        records = data['list']['data']['items']
        assert data['list']['data']['total'] == len(records)
        docs = []
        for i, item in enumerate(records):
            assert isinstance(item['id'], str) and isinstance(item['title'], str)
            docs.append({**item, 'attachments': [urljoin('http://fixture.invalid', a)
                                                for a in item['attachments']],
                         'evidence': {'json_pointer': f'/list/data/items/{i}',
                                      'raw_hash': sha256_hex(response.body)}})
        OUT['json'] = {'documents': docs}

    def parse_txt(self, response):
        text = response.text
        title = text.splitlines()[0]
        span = [0, len(title)]
        OUT['txt'] = {'title': title, 'body_text': text, 'char_span': span,
                      'raw_hash': sha256_hex(response.body),
                      'evidence_valid': text[span[0]:span[1]] == title}

    def parse_dynamic(self, response):
        # Text in script source does NOT count as visible target data.
        visible = response.css('#data::text').get() or ''
        methods = response.meta.get('playwright_page_methods', [])
        frames = methods[-1].result if methods else []
        OUT['dynamic'] = {'target_found': visible == 'JS_RECORD_001',
                          'iframe_target_found': any('IFRAME_RECORD_002' in f['text']
                                                     for f in frames),
                          'frames': frames,
                          'iframe_urls': response.css('iframe::attr(src)').getall()}
        (RESULTS / 'raw' / f'fixture_dynamic_{self.mode}.html').write_bytes(response.body)

    def failed(self, failure):
        OUT.setdefault('failures', []).append({'type': failure.type.__name__,
                                               'path': urlparse(failure.request.url).path})


def summarize_versions():
    docs = OUT['versions']
    if len(docs) != 4:
        return
    OUT['observation_count'] = len(docs)
    OUT['revision_count'] = len({d['content_hash'] for d in docs.values()})
    OUT['diffs'] = {}
    for first, last in [('a', 'b'), ('b', 'c')]:
        old, new = docs[first]['Result'], docs[last]['Result']
        OUT['diffs'][first + '_' + last] = {
            field: list(difflib.unified_diff(old[field].splitlines(), new[field].splitlines()))
            for field in ('title', 'body_text')}
    original = docs['a']['Result']
    changed = {**original, 'title': original['title'] + '（更正）'}
    OUT['title_change_detected'] = content_hash(original) != content_hash(changed)
    # Date text is legal business content; do not strip it as display noise.
    changed = {**original, 'body_text': original['body_text'] + '\n履行日期：2026-04-17 22:41:07'}
    OUT['body_timestamp_preserved'] = content_hash(original) != content_hash(changed)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'raw'
    if mode not in ('raw', 'playwright'):
        raise SystemExit('raw | playwright')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.perf_counter()
    settings = {'CONCURRENT_REQUESTS': 1, 'CONCURRENT_REQUESTS_PER_DOMAIN': 1,
                'DOWNLOAD_DELAY': 3, 'DOWNLOAD_DELAY_JITTER': 0,
                'RETRY_ENABLED': False, 'REDIRECT_ENABLED': False,
                'ROBOTSTXT_OBEY': False,  # author-owned synthetic loopback only
                'HTTPPROXY_ENABLED': False, 'TELNETCONSOLE_ENABLED': False,
                'REMOTE_CONTROL_ENABLED': False, 'COOKIES_ENABLED': False,
                'DOWNLOAD_TIMEOUT': 15, 'DOWNLOAD_MAXSIZE': 1_000_000,
                'LOG_LEVEL': 'WARNING'}
    if mode == 'playwright':
        settings.update({
            'DOWNLOAD_HANDLERS': {'http': 'scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler'},
            'TWISTED_REACTOR': 'twisted.internet.asyncioreactor.AsyncioSelectorReactor',
            'PLAYWRIGHT_LAUNCH_OPTIONS': {'headless': True, 'channel': 'chrome'},
            'PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT': 20000,
            'PLAYWRIGHT_MAX_PAGES_PER_CONTEXT': 1,
            'PLAYWRIGHT_CONTEXTS': {'default': {'service_workers': 'block'}},
        })
    proc = CrawlerProcess(settings)
    crawler = proc.create_crawler(FixtureSpider)
    try:
        proc.crawl(crawler, mode=mode, base=f'http://127.0.0.1:{server.server_port}')
        proc.start()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    summarize_versions()
    OUT.update({'mode': mode, 'finished_at': now_iso(),
                'elapsed_s': time.perf_counter() - started, 'local_requests': REQUESTS,
                'browser_requests': BROWSER_REQUESTS, 'blocked_external': BLOCKED_EXTERNAL,
                'runner_peak_rss_bytes_macos': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'memory_scope': 'Python process only; excludes browser processes',
                'scrapy_stats': crawler.stats.get_stats()})
    write_json(RESULTS / f'fixtures_{mode}.json', OUT)
    print(json.dumps({'mode': mode, 'local_requests': len(REQUESTS),
                      'elapsed_s': OUT['elapsed_s'], 'failures': OUT.get('failures', [])}))
    return int(bool(OUT.get('failures')))


if __name__ == '__main__':
    sys.exit(main())
