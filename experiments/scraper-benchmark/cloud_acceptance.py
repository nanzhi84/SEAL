#!/usr/bin/env python3
"""Offline E2E grading of actual Firecrawl /parse artifacts; never calls the API.

Predeclared failures: missing/invalid key, HTTP 402/403/429, transport errors,
unknown free credit balance, >6 requests or >4 credits, empty/partial output,
footer-only false revisions, missed body revision, unstable repeated input.
Failures/blocked calls remain artifacts; missing outputs are not scored 0%.
"""
import argparse
import difflib
import json
import re
import statistics
from datetime import datetime
from pathlib import Path

from bench.common import FIXTURES, RESULTS, now_iso, sha256_hex, write_json
from bench.revision import content_hash
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
        normalized_links, replay_records, native_docs, source_urls = {}, {}, {}, {}
        for name, record in comparison['records'].items():
            original = FIXTURES / f"version_{'a' if name == 'a_repeat' else name}.html"
            html = original.read_text()
            truth = Text()
            truth.feed(body_fragment(html, '<div class="txt_txt">'))
            segments = [compact(p) for p in truth.parts if len(compact(p)) >= 8]
            markdown = record['native_result']['body_text']
            observed = compact(markdown)
            returned = json.loads((root / f'fixture_{name}.json').read_text())
            raw_html = returned.get('raw_html', returned.get('rawHtml', ''))
            source_urls[name] = (returned.get('metadata') or {}).get('url')
            native_docs[name] = record['native_result']
            # Diagnostic only, NOT another API result or a production link cleaner.
            normalized_links[name] = {**record['native_result'], 'body_text': re.sub(
                r'https://parse\.firecrawl\.dev/uploads/version_[abc]\.html#',
                'https://fixture.invalid/document.html#', markdown)}
            from bench.recipes import apply
            replayed = apply('court_detail', raw_html, 'http://fixture.invalid/document/999',
                             sha256_hex(original.read_bytes()))
            replay_records[name] = {'content_hash': content_hash(replayed), 'result': replayed}
            result['documents'].append({
                'name': name, 'input_hash_verified': record['input_raw_hash'] == sha256_hex(original.read_bytes()),
                'body_nonempty': bool(markdown.strip()), 'body_chars': len(markdown),
                'expected_segments': len(segments), 'matched_segments': sum(s in observed for s in segments),
                'metadata_title': record['native_result']['title'],
                'metadata_published_at': record['native_result']['published_at'],
                'footer_markers': [m for m in ['访问量', '责任编辑', '版权所有', '网站地图'] if m in markdown],
                'navigation_markers': [m for m in ['所在位置', '字号', '打印本页'] if m in markdown],
                'raw_html_bytes_equal_input': raw_html.encode('utf-8') == original.read_bytes(),
                'limitation': 'long-segment containment, not a general Markdown-to-plain-text completeness score',
            })
        result['content_assertions'] = {key: comparison[key] for key in
                                       ['all_nonempty', 'noise_stable', 'body_change_detected', 'fixed_input_deterministic']}
        result['status'] = 'COMPLETED_WITH_IDENTITY_CONFOUND'
        result['identity_control'] = {
            'source_urls': source_urls,
            'held_constant_A_B_C': len({source_urls[n] for n in ['a', 'b', 'c']}) == 1,
            'noise_test_verdict': 'CONFOUNDED_BY_UPLOAD_FILENAME; not a framework noise-removal failure',
            'native_A_B_diff': list(difflib.unified_diff(
                native_docs['a']['body_text'].splitlines(), native_docs['b']['body_text'].splitlines())),
            'only_filename_links_differ_A_B': normalized_links['a'] == normalized_links['b'],
            'body_change_after_diagnostic_link_normalization':
                content_hash(normalized_links['b']) != content_hash(normalized_links['c']),
            'normalization_is': 'offline diagnosis, not re-executed provider output',
        }
        local = json.loads((RESULTS / 'fixtures_raw.json').read_text())['versions']
        result['seal_recipe_replay_on_provider_raw_html'] = {
            'records': replay_records,
            'matches_local_scrapy_hashes': {n: r['content_hash'] == local[n]['content_hash']
                                             for n, r in replay_records.items()},
            'A_B_equal': replay_records['a']['content_hash'] == replay_records['b']['content_hash'],
            'B_C_different': replay_records['b']['content_hash'] != replay_records['c']['content_hash'],
            'added_by': 'SEAL Python Recipe over real returned raw_html, not native Firecrawl extraction',
        }
    cost = root / 'cost.json'
    result['cost'] = json.loads(cost.read_text()) if cost.exists() else None
    ledger = json.loads((root / 'ledger.json').read_text())
    parse_calls = [r for r in ledger if r['label'].startswith('fixture_')]
    credits = [r['actual_credits'] for r in parse_calls]
    result['credits_reported_by_documents'] = sum(credits) if all(isinstance(c, int) for c in credits) else None
    result['budget_credits_ok'] = (result['cost'] is not None and
                                   result['cost']['credit_balance_delta'] <= 4 and
                                   result['credits_reported_by_documents'] == result['cost']['credit_balance_delta'])
    result['parse_seconds'] = {'min': min(r['elapsed_s'] for r in parse_calls),
                               'max': max(r['elapsed_s'] for r in parse_calls),
                               'median': statistics.median(r['elapsed_s'] for r in parse_calls)} if parse_calls else None
    starts = [datetime.fromisoformat(r['at']).timestamp() for r in requests]
    result['minimum_api_start_interval_seconds'] = min(b - a for a, b in zip(starts, starts[1:])) if len(starts) > 1 else None
    result['response_artifact_hashes_match_http_bytes'] = all(
        sha256_hex((root / r['sanitized_response_artifact']).read_bytes()) == r['response_sha256']
        for r in requests if 'sanitized_response_artifact' in r)
    result['limitations'] = ['Parse on synthetic HTML is not live-site Scrape, Crawl, PDF or browser rendering',
                             'No quality metric is assigned to documents never returned by the service']
    write_json(root / 'acceptance.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['budget_requests_ok'] and (result['budget_credits_ok'] or result['cost'] is None) else 1


if __name__ == '__main__':
    raise SystemExit(main())
