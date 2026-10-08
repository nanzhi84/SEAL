#!/usr/bin/env python3
"""Official Firecrawl SDK runner. No emulation and no implicit spending.

status: zero-network credential / SDK checks.
fixtures: official /parse uploads A,B,C,A (fixed HTML inputs).
scrape: same A/B/C/D/E targets; F/G are excluded due to access restrictions.
list: two court list scrapes plus at most five detail scrapes; official links
      output, no home-grown Firecrawl crawler or markdown extraction.

The inherited campaign is LOCKED because subrequests were not metered. A new
operator-authorized campaign may supply --authorization PATH, specifying a
verified free-only account and gateway-enforced request/rate/policy controls.
No paid calls, proxy escalation, LLM extraction, retries, or background crawls.
SDK/API success is NOT automatically a content-quality PASS.
"""
import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from firecrawl import Firecrawl
from firecrawl.v2.types import ScrapeOptions

from bench.common import FIXTURES, RESULTS, now_iso, sha256_hex, write_json
from bench.targets import BY_ID, FIRECRAWL_SCRAPE

FORMATS = ['markdown', 'html', 'rawHtml', 'links']
BASE = {'formats': FORMATS, 'only_main_content': True,
        'timeout': 30000, 'max_age': 0, 'store_in_cache': False, 'proxy': 'basic'}


def status(mode, reason=None):
    key = bool(os.environ.get('FIRECRAWL_API_KEY'))
    hosted = bool(os.environ.get('FIRECRAWL_API_URL'))
    record = {'at': now_iso(), 'status': 'BLOCKED', 'requested_mode': mode,
              'reason': reason or ('MISSING_FIRECRAWL_API_KEY' if not key else
                                   'HISTORICAL_NETWORK_BUDGET_UNKNOWN'),
              'key_present': key, 'custom_endpoint_configured': hosted,
              'cloud_scrape': 'BLOCKED', 'cloud_parse_fixtures': 'BLOCKED',
              'self_hosted': 'NOT_TESTED' if not hosted else 'BLOCKED',
              'external_requests_this_run': 0, 'credits_this_run': 0,
              'historical_credit_probe': 'DECLARED_ONLY; no response artifact retained',
              'underlying_provider_requests': 'UNKNOWN / no job started this run'}
    write_json(RESULTS / 'firecrawl_status.json', record)
    print(json.dumps(record, indent=2))
    return record


class Calls:
    """Small serial API ledger. Reserve before call, persist even on exception.

Does not claim that an API call equals one website network request. An external
trusted gateway must enforce origin subrequest budgets for live-site modes.
"""
    def __init__(self, authorization, mode):
        auth = json.loads(Path(authorization).read_text())
        required = ['new_campaign_explicitly_approved', 'free_only_verified',
                    'no_auto_recharge', 'robots_and_site_policy_reviewed']
        if not all(auth.get(k) is True for k in required):
            raise ValueError('authorization incomplete')
        if mode != 'fixtures' and not auth.get('gateway_enforces_origin_policy'):
            raise ValueError('Cloud origin subrequests/rate/robots cannot be audited here')
        self.max_calls = min(80, int(auth['api_request_budget']))
        self.max_credits = min(30, int(auth['free_credit_budget']))
        # Caller must create a NEW directory; cannot silently reset a spent budget.
        self.root = RESULTS / ('firecrawl_' + auth['campaign_id'])
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', auth['campaign_id']):
            raise ValueError('invalid campaign_id')
        self.root.mkdir(exist_ok=False)
        self.client = Firecrawl(api_key=os.environ['FIRECRAWL_API_KEY'],
                                api_url=os.environ.get('FIRECRAWL_API_URL',
                                                       'https://api.firecrawl.dev'),
                                timeout=45, max_retries=0)
        self.records = []
        self.reserved_credits = 0
        self.allowed = set(auth.get('allowed_urls', []))
        write_json(self.root / 'authorization.json', auth)

    def call(self, label, credits, function):
        if len(self.records) + 1 > self.max_calls or self.reserved_credits + credits > self.max_credits:
            raise RuntimeError('BUDGET_BLOCKED before dispatch')
        self.reserved_credits += credits
        rec = {'label': label, 'at': now_iso(), 'status': 'DISPATCH_RESERVED',
               'credits_reserved_upper_bound': credits, 'actual_credits': None}
        self.records.append(rec)
        write_json(self.root / 'ledger.json', self.records)
        started = time.perf_counter()
        try:
            value = function()
            obj = value.model_dump(mode='json', by_alias=True) if hasattr(value, 'model_dump') else value
            # Persist document outputs, but never account/user identifiers from usage.
            if label.startswith('usage_'):
                obj = {k: v for k, v in obj.items() if k in (
                    'remaining_credits', 'remainingCredits', 'plan_credits', 'planCredits')}
            write_json(self.root / f'{label}.json', obj)
            rec['status'] = 'RETURNED_NOT_QUALITY_GRADED'
            meta = obj.get('metadata') or {}
            rec['actual_credits'] = meta.get('creditsUsed', meta.get('credits_used'))
            rec['artifact'] = f'{label}.json'
            return obj
        except Exception as exc:
            # Provider exception text can echo headers / credentials. Store type only.
            rec['status'] = 'FAILED'
            rec['error_type'] = type(exc).__name__
            rec['http_status'] = getattr(exc, 'status_code', None)
            rec['failure_category'] = {401: 'AUTHENTICATION', 402: 'PAYMENT_REQUIRED',
                                       403: 'ACCESS_RESTRICTED', 429: 'RATE_LIMIT'}.get(
                                           rec['http_status'], 'API_OR_TRANSPORT_ERROR')
            raise RuntimeError('Firecrawl failed; no retry, see ledger') from None
        finally:
            rec['elapsed_s'] = time.perf_counter() - started
            write_json(self.root / 'ledger.json', self.records)
            time.sleep(3)

    def scrape(self, label, url, main=True, pdf=False):
        if url not in self.allowed:
            raise RuntimeError('URL_NOT_AUTHORIZED')
        opts = {**BASE, 'only_main_content': main}
        if pdf:
            opts['parsers'] = [{'type': 'pdf', 'max_pages': 5, 'pages': True, 'blocks': True}]
        data = self.call(label, 6 if pdf else 1, lambda: self.client.scrape(url, **opts))
        metadata = data.get('metadata') or {}
        links = data.get('links') or []
        normalized = {'source_url': url, 'title': metadata.get('title') or '',
                      'published_at': metadata.get('publishedTime', metadata.get('published_time')),
                      'body_text': data.get('markdown') or '',
                      'body_representation': 'markdown (not stripped to plain text)',
                      'document_links': links,
                      'attachments': [x for x in links if re.search(r'\.(pdf|docx?|xlsx?|txt)(\?|$)', x, re.I)],
                      'quality_status': 'NOT_GRADED',
                      'provider_status': metadata.get('statusCode', metadata.get('status_code')),
                      'raw_snapshot_note': 'rawHtml is provider-returned HTML, NOT proven wire bytes'}
        write_json(self.root / f'{label}.normalized.json', normalized)
        if metadata.get('statusCode', metadata.get('status_code')) in (401, 403, 412, 429):
            raise RuntimeError('ACCESS_RESTRICTED_STOP')
        return data


def run_fixtures(calls):
    from bench.revision import content_hash
    records = {}
    for name in ['a', 'b', 'c', 'a_repeat']:
        path = FIXTURES / ('version_' + ('a' if name == 'a_repeat' else name) + '.html')
        options = ScrapeOptions(formats=FORMATS, only_main_content=True, timeout=30000)
        data = calls.call('fixture_' + name, 1, lambda: calls.client.parse(path, filename='document.html', options=options))
        meta = data.get('metadata') or {}
        doc = {'title': meta.get('title') or '', 'published_at': meta.get('publishedTime', meta.get('published_time')),
               'body_text': data.get('markdown') or ''}
        records[name] = {'input_raw_hash': sha256_hex(path.read_bytes()),
                         'content_hash': content_hash(doc), 'native_result': doc}
    a, b, c, repeat = [records[k] for k in ['a', 'b', 'c', 'a_repeat']]
    write_json(calls.root / 'fixture_comparison.json', {
        'records': records, 'sample_class': 'SYNTHETIC_CLOUD_PARSE_NOT_LIVE_SITE',
        'all_nonempty': all(r['native_result']['body_text'] for r in records.values()),
        'noise_stable': a['content_hash'] == b['content_hash'],
        'body_change_detected': b['content_hash'] != c['content_hash'],
        'fixed_input_deterministic': a['content_hash'] == repeat['content_hash'],
        'hash_layer': 'SEAL-added; native markdown kept unchanged; no fabricated field evidence',
    })


def run_list(calls):
    first = BY_ID['A_list_p1']['url']
    page = calls.scrape('A_list_p1', first, main=False)
    # Use Firecrawl native link output. Only pagination selector is site policy.
    from parsel import Selector
    selector = Selector(text=page.get('rawHtml') or page.get('raw_html') or '')
    nxt = selector.css('div.page li.next a::attr(href), a.next::attr(href)').get()
    detail_urls = list(dict.fromkeys(u for u in page.get('links', [])
                                    if re.search(r'/shenpan/xiangqing/\d+\.html$', u)))
    if nxt:
        calls.scrape('A_list_p2', urljoin(first, nxt), main=False)
    edges = []
    for i, url in enumerate(detail_urls[:5], 1):
        calls.scrape(f'A_detail_{i}', url)
        edges.append({'parent_url': first, 'detail_url': url})
    write_json(calls.root / 'discovery.json', {'edges': edges, 'coverage': 'UNKNOWN until independently graded'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', default='status', choices=['status', 'fixtures', 'scrape', 'list'])
    parser.add_argument('--authorization', help='new, explicitly approved campaign; see README')
    args = parser.parse_args()
    # Validate installed SDK types locally, without creating an HTTP request.
    ScrapeOptions(**BASE)
    ScrapeOptions(parsers=[{'type': 'pdf', 'max_pages': 5, 'pages': True, 'blocks': True}])
    if not os.environ.get('FIRECRAWL_API_KEY') or args.mode == 'status' or not args.authorization:
        status(args.mode)
        return 0 if args.mode == 'status' else 2
    calls = Calls(args.authorization, args.mode)
    if args.mode == 'fixtures':
        from bench.firecrawl_http import APIMeter
        with APIMeter(calls.root, os.environ['FIRECRAWL_API_KEY']):
            return run_authorized(calls, args.mode)
    return run_authorized(calls, args.mode)


def run_authorized(calls, mode):
    # Free-only / auto-recharge are operator assertions, not proven by balance.
    before = calls.call('usage_before', 0, calls.client.get_credit_usage)
    remaining = before.get('remainingCredits', before.get('remaining_credits'))
    required = {'fixtures': 4, 'scrape': 10, 'list': 7}[mode]
    if remaining is None or remaining < required or calls.max_credits < required:
        raise SystemExit('BLOCKED: insufficient verified free credits; no content calls')
    if mode == 'fixtures':
        run_fixtures(calls)
    elif mode == 'list':
        run_list(calls)
    else:
        for tid in FIRECRAWL_SCRAPE:
            calls.scrape(tid, BY_ID[tid]['url'], pdf=(tid == 'C_pdf'))
    after = calls.call('usage_after', 0, calls.client.get_credit_usage)
    balance = after.get('remainingCredits', after.get('remaining_credits'))
    write_json(calls.root / 'cost.json', {
        'credit_balance_delta': remaining - balance if balance is not None else None,
        'caveat': 'Account delta is attributable only if no other jobs share this account',
        'cash_cost': 'free-only account verified by operator; no paid plan activation',
        'api_calls': len(calls.records),
        'underlying_origin_requests': 0 if mode == 'fixtures' else 'gateway evidence required',
    })
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
