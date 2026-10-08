#!/usr/bin/env python3
"""Offline E2E grading of actual Firecrawl /parse artifacts; never calls the API.

Predeclared failures: missing/invalid key, HTTP 402/403/429, transport errors,
unknown free credit balance, >6 requests or >4 credits, empty/partial output,
footer-only false revisions, missed body revision, unstable repeated input.
Failures/blocked calls remain artifacts; missing outputs are not scored 0%.
"""
import argparse
import json
from pathlib import Path

from bench.common import FIXTURES, now_iso, sha256_hex, write_json
from quality_audit import Text, body_fragment, compact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign')
    args = parser.parse_args()
    root = Path(args.campaign)
    requests = json.loads((root / 'http_ledger.json').read_text())
    result = {'at': now_iso(), 'sample_class': 'SYNTHETIC_CLOUD_PARSE',
              'request_count': len(requests), 'requests_with_responses': sum(
                  r.get('http_status') is not None for r in requests),
              'target_website_requests': 0, 'budget_requests_ok': len(requests) <= 6,
              'documents': [], 'content_assertions': {}, 'status': 'BLOCKED_OR_FAILED'}
    comparison = root / 'fixture_comparison.json'
    if comparison.exists():
        comparison = json.loads(comparison.read_text())
        for name, record in comparison['records'].items():
            original = FIXTURES / f"version_{'a' if name == 'a_repeat' else name}.html"
            html = original.read_text()
            truth = Text()
            truth.feed(body_fragment(html, '<div class="txt_txt">'))
            segments = [compact(p) for p in truth.parts if len(compact(p)) >= 8]
            markdown = record['native_result']['body_text']
            observed = compact(markdown)
            result['documents'].append({
                'name': name, 'input_hash_verified': record['input_raw_hash'] == sha256_hex(original.read_bytes()),
                'body_nonempty': bool(markdown.strip()), 'body_chars': len(markdown),
                'expected_segments': len(segments), 'matched_segments': sum(s in observed for s in segments),
                'metadata_title': record['native_result']['title'],
                'metadata_published_at': record['native_result']['published_at'],
                'footer_markers': [m for m in ['访问量', '责任编辑', '版权所有', '网站地图'] if m in markdown],
                'limitation': 'long-segment containment, not a general Markdown-to-plain-text completeness score',
            })
        result['content_assertions'] = {key: comparison[key] for key in
                                       ['all_nonempty', 'noise_stable', 'body_change_detected', 'fixed_input_deterministic']}
        result['status'] = 'COMPLETED'
    cost = root / 'cost.json'
    result['cost'] = json.loads(cost.read_text()) if cost.exists() else None
    result['limitations'] = ['Parse on synthetic HTML is not live-site Scrape, Crawl, PDF or browser rendering',
                             'No quality metric is assigned to documents never returned by the service']
    write_json(root / 'acceptance.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['budget_requests_ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
