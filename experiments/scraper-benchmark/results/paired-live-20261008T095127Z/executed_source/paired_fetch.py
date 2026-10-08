#!/usr/bin/env python3
"""One real Scrapy Request/Response. Called serially by the paired coordinator.

No redirects/retries/robots duplicate fetches/HTTP cache. Shared robots decisions
are made by the coordinator for both engines. No credential is needed here.
"""
import argparse
import json
import resource
import time
from pathlib import Path

import scrapy
from scrapy.crawler import CrawlerProcess

UA = 'SEAL-Benchmark/1.0 (bounded public-source evaluation)'
LANGUAGE = 'zh-CN,zh;q=0.9,en;q=0.8'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('output')
    args = ap.parse_args()
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    result = {'requested_url': args.url, 'status': 'TRANSPORT_ERROR', 'http_status': None}

    class Once(scrapy.Spider):
        name = 'paired_once'
        async def start(self):
            yield scrapy.Request(args.url, headers={'User-Agent': UA, 'Accept-Language': LANGUAGE},
                                 callback=self.parse, errback=self.failed,
                                 meta={'handle_httpstatus_all': True})

        def parse(self, response):
            (root / 'response.bin').write_bytes(response.body)
            result.update({'status': 'FETCHED', 'http_status': response.status,
                           'final_url': response.url, 'raw_path': 'response.bin',
                           'bytes': len(response.body), 'flags': list(response.flags),
                           'encoding': getattr(response, 'encoding', None),
                           'download_latency_s': response.meta.get('download_latency'),
                           'headers': {name: response.headers.get(name, b'').decode('latin-1') for name in
                                       ['Content-Type', 'Content-Encoding', 'ETag', 'Last-Modified', 'Location']}})

        def failed(self, failure):
            result['error_type'] = failure.type.__name__

    settings = {'CONCURRENT_REQUESTS': 1, 'CONCURRENT_REQUESTS_PER_DOMAIN': 1,
                'DOWNLOAD_DELAY': 3, 'DOWNLOAD_DELAY_JITTER': 0,
                'DOWNLOAD_TIMEOUT': 45, 'DOWNLOAD_MAXSIZE': 15_000_000,
                'RETRY_ENABLED': False, 'REDIRECT_ENABLED': False,
                'METAREFRESH_ENABLED': False, 'ROBOTSTXT_OBEY': False,
                'HTTPCACHE_ENABLED': False, 'COOKIES_ENABLED': False,
                'DOWNLOADER_CLIENTCONTEXTFACTORY': 'scrapy.core.downloader.contextfactory.BrowserLikeContextFactory',
                'TELNETCONSOLE_ENABLED': False, 'REMOTE_CONTROL_ENABLED': False,
                'LOG_ENABLED': False}
    started = time.perf_counter()
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(Once)
    process.crawl(crawler)
    process.start()
    result.update({'worker_elapsed_s': time.perf_counter() - started,
                   'scrapy_request_count': crawler.stats.get_value('downloader/request_count', 0),
                   'python_peak_rss_bytes_macos': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
    (root / 'observation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
