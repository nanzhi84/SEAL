#!/usr/bin/env python3
"""Approved same-URL paired campaign. No implicit retries or reusable budgets.

Only this coordinator dispatches network work. Scoring/replay is offline.
Independent client/API scope approved by the operator; provider subrequests
remain UNKNOWN. F/G never enter the allowlist.
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse, urljoin

import requests
from firecrawl import Firecrawl
from protego import Protego

from bench.common import HERE, RESULTS, now_iso, read_json, write_json, sha256_hex
from bench.paired_meter import Meter
from paired_fetch import UA, LANGUAGE

RESTRICTED = {401, 403, 412, 429}
HEADERS = {'User-Agent': UA, 'Accept-Language': LANGUAGE}


def dump(value):
    return value.model_dump(mode='json', by_alias=False) if hasattr(value, 'model_dump') else value


def policy(source, cases, root, meter):
    origin = urlparse(cases[0]['url'])
    url = f'{origin.scheme}://{origin.netloc}/robots.txt'
    out = {'source': source, 'status': 'BLOCKED_POLICY', 'requests': [], 'decisions': {}}
    for hop in range(3):
        meter.context = ('robots', url, 0)
        try:
            response = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=False)
        except requests.RequestException as exc:
            out['error_type'] = type(exc).__name__
            break
        out['requests'].append({'url': url, 'http_status': response.status_code})
        if response.status_code in RESTRICTED or response.status_code >= 500:
            out['reason'] = 'POLICY_ACCESS_RESTRICTED_OR_UNREACHABLE'
            break
        if 300 <= response.status_code < 400:
            target = urljoin(url, response.headers.get('Location', ''))
            if urlparse(target).netloc != origin.netloc or urlparse(target).scheme != 'https':
                out['reason'] = 'ROBOTS_CROSS_ORIGIN_REDIRECT_NOT_APPROVED'
                break
            url = target
            continue
        if response.status_code in (404, 410):
            out.update(status='ALLOWED_ROBOTS_UNAVAILABLE_4XX', crawl_delay_s=3.01,
                       decisions={c['id']: True for c in cases})
            break
        if response.status_code == 200:
            body = response.content
            (root / f'robots_{source}.txt').write_bytes(body)
            text = body.decode('utf-8', errors='replace')
            if '<html' in text.lower() and not re.search(r'^\s*user-agent\s*:', text, re.I | re.M):
                out['reason'] = 'ROBOTS_HTML_SOFT_ERROR_NOT_AN_AUTHORITATIVE_RULESET'
                break
            rules = Protego.parse(text)
            delay = max(3.01, rules.crawl_delay(UA) or 0, rules.crawl_delay('Firecrawl') or 0)
            out.update(status='ROBOTS_RULES_PARSED', crawl_delay_s=delay,
                       robots_sha256=sha256_hex(body),
                       decisions={c['id']: rules.can_fetch(c['url'], UA) and
                                  rules.can_fetch(c['url'], 'Firecrawl') for c in cases})
            if delay > 60:
                out.update(status='BLOCKED_POLICY', reason='CRAWL_DELAY_EXCEEDS_BOUNDED_RUN')
            break
        out['reason'] = 'UNHANDLED_POLICY_RESPONSE'
        break
    return out


def scrapy_fetch(case, root, meter):
    leaf = root / 'scrapy' / case['id']
    item, started = meter.reserve('scrapy', case['url'], case['url'])
    env = {k: v for k, v in os.environ.items() if k not in ('FIRECRAWL_API_KEY', 'PI_SESSION_FILE')}
    try:
        result = subprocess.run([sys.executable, str(HERE / 'paired_fetch.py'), case['url'], str(leaf)],
                                env=env, capture_output=True, timeout=60, cwd=HERE)
        if result.returncode:
            raise RuntimeError('SCRAPY_WORKER_FAILED')
        out = read_json(leaf / 'observation.json')
        if out.get('raw_path'):
            out['raw_path'] = str((leaf / out['raw_path']).relative_to(root))
        out['started_at'] = item['started_at']
        out['ledger_id'] = item['id']
        meter.finish(item, started, status='RESPONSE' if out['http_status'] else 'TRANSPORT_ERROR',
                     http_status=out['http_status'], scrapy_request_count=out['scrapy_request_count'])
        if out['scrapy_request_count'] > 1:
            raise RuntimeError('UNEXPECTED_SCRAPY_REQUEST_COUNT')
        return out
    except Exception as exc:
        if item['status'] == 'DISPATCH_RESERVED':
            meter.finish(item, started, status='TRANSPORT_ERROR', error_type=type(exc).__name__)
        return {'status': 'TRANSPORT_ERROR', 'error_type': type(exc).__name__,
                'requested_url': case['url'], 'started_at': item['started_at']}


def cloud_fetch(case, root, meter, client, original_pdf=False):
    pdf = case['function_id'] == 'pdf_ingestion'
    schema_fields = read_json(root / 'protocol.json')['function_contracts']['company_fields']
    formats = ['markdown', 'html', 'rawHtml', 'links']
    credits = 6 if pdf and not original_pdf else 1
    if case['function_id'] == 'company_fields':
        formats.append({'type': 'json', 'schema': {
            'type': 'object', 'properties': {key: {'type': 'string'} for key in schema_fields},
            'required': schema_fields},
            'prompt': 'Extract exactly the displayed company fields; preserve source spelling and date text. '
                      'Join multiple SIC codes or previous names with |. Do not infer missing data.'})
        credits = 5
    opts = {'formats': formats, 'headers': HEADERS, 'only_main_content': case['role'] == 'detail',
            'timeout': 45000, 'max_age': 0, 'store_in_cache': False, 'proxy': 'basic',
            'skip_tls_verification': False}
    if pdf:
        opts['parsers'] = [] if original_pdf else [
            {'type': 'pdf', 'max_pages': 5, 'pages': True, 'blocks': True, 'mode': 'fast'}]
    meter.context = ('firecrawl_cloud', case['url'], credits)
    leaf = root / 'firecrawl_cloud' / (case['id'] + ('_original' if original_pdf else ''))
    leaf.mkdir(parents=True)
    write_json(leaf / 'options.json', opts)
    started = time.perf_counter()
    try:
        data = dump(client.scrape(case['url'], **opts))
        write_json(leaf / 'document.json', data)
        meta = data.get('metadata') or {}
        raw = data.get('raw_html') or ''
        raw_path = leaf / 'response.html'
        raw_path.write_text(raw)
        if original_pdf:
            # Disabling PDF parsing returns original base64 per official contract.
            for value in (raw, data.get('markdown') or ''):
                value = re.sub(r'^data:application/pdf;base64,', '', value)
                try:
                    decoded = base64.b64decode(value, validate=True)
                except (ValueError, TypeError):
                    continue
                if decoded.startswith(b'%PDF-'):
                    raw_path = leaf / 'response.pdf'
                    raw_path.write_bytes(decoded)
                    break
        return {'status': 'FETCHED', 'requested_url': case['url'],
                'http_status': meta.get('status_code'), 'final_url': meta.get('url') or meta.get('source_url'),
                'raw_path': str(raw_path.relative_to(root)),
                'document_path': str((leaf / 'document.json').relative_to(root)),
                'elapsed_s': time.perf_counter()-started, 'credits_used': meta.get('credits_used'),
                'started_at': meter.data['requests'][-1]['started_at'],
                'cache_state': meta.get('cache_state'), 'provider_error': meta.get('error'),
                'ledger_id': meter.data['requests'][-1]['id']}
    except Exception as exc:
        item = meter.data['requests'][-1]
        return {'status': 'API_ERROR', 'error_type': type(exc).__name__,
                'api_status': item.get('http_status'), 'requested_url': case['url'],
                'started_at': item['started_at'], 'ledger_id': item['id']}


def access_restricted(observation, root):
    if observation.get('http_status') in RESTRICTED:
        return True
    if observation.get('status') == 'API_ERROR':
        return observation.get('api_status') in RESTRICTED
    path = root / observation.get('raw_path', '_missing')
    if path.exists():
        first = path.read_bytes()[:20000].decode('utf-8', 'ignore')
        title = re.search(r'<title[^>]*>(.*?)</title>', first, re.I | re.S)
        if title and re.search(r'access denied|403 forbidden|安全验证|访问验证|verify you are human', title[1], re.I):
            return True
    return False


def usage(client, root, meter, name):
    meter.context = ('firecrawl_cloud', None, 0)
    data = dump(client.get_credit_usage())
    safe = {key: data.get(key) for key in ('remaining_credits', 'plan_credits')}
    write_json(root / (name + '.json'), safe)
    return safe['remaining_credits']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--campaign', required=True)
    args = ap.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,50}', args.campaign):
        raise ValueError('invalid campaign id')
    protocol = read_json(HERE / 'paired_protocol.json')
    if not protocol['budget']['scope_change_confirmed_by_user'] or not os.getenv('FIRECRAWL_API_KEY'):
        raise RuntimeError('MISSING_APPROVAL_OR_CREDENTIAL')
    root = RESULTS / args.campaign
    root.mkdir(exist_ok=False)
    shutil.copyfile(HERE / 'paired_protocol.json', root / 'protocol.json')
    (root / 'executed_source').mkdir()
    sources = ['paired_live.py', 'paired_fetch.py', 'paired_acceptance.py', 'bench/paired_meter.py',
               'bench/recipes.py', 'bench/extract.py', 'bench/pdfio.py']
    for path in sources:
        shutil.copyfile(HERE / path, root / 'executed_source' / path.replace('/', '_'))
    run = {'campaign_id': args.campaign, 'started_at': now_iso(), 'status': 'RUNNING',
           'provider_origin_subrequests': 'UNKNOWN', 'cases': {}, 'policies': {},
           'authorization': 'User explicitly agreed to <=80 client HTTP/API, <=30 free credits; no auto-payment',
           'source_sha256': {p: sha256_hex((HERE / p).read_bytes()) for p in sources}}
    for case in protocol['cases']:
        run['cases'][case['id']] = {**case, 'engines': {e: {'status': 'PENDING'} for e in protocol['engines']}}
    def save():
        write_json(root / 'run.json', run)
    save()
    client = Firecrawl(api_key=os.environ['FIRECRAWL_API_KEY'], api_url='https://api.firecrawl.dev',
                       timeout=65, max_retries=0)
    with Meter(root, os.environ['FIRECRAWL_API_KEY'], [c['url'] for c in protocol['cases']]) as meter:
        before = None
        stopped = set()
        try:
            before = usage(client, root, meter, 'usage_before')
            if before is None or before < 30:
                raise RuntimeError('INSUFFICIENT_VERIFIED_BALANCE')
            for source in dict.fromkeys(c['source'] for c in protocol['cases']):
                subset = [c for c in protocol['cases'] if c['source'] == source]
                run['policies'][source] = policy(source, subset, root, meter)
                print('robots', source, run['policies'][source]['status'], flush=True)
                save()
            for index, case in enumerate(protocol['cases']):
                result = run['cases'][case['id']]
                rules = run['policies'][case['source']]
                if not rules['decisions'].get(case['id']) or rules['status'] == 'BLOCKED_POLICY':
                    result['engines'] = {e: {'status': 'BLOCKED_POLICY'} for e in protocol['engines']}
                    save()
                    continue
                meter.delay = rules['crawl_delay_s']
                order = protocol['engines'] if index % 2 == 0 else list(reversed(protocol['engines']))
                for engine in order:
                    if case['source'] in stopped:
                        result['engines'][engine] = {'status': 'BLOCKED_ACCESS_STOP'}
                        continue
                    observation = (scrapy_fetch(case, root, meter) if engine == 'scrapy' else
                                   cloud_fetch(case, root, meter, client))
                    result['engines'][engine] = observation
                    print(case['id'], engine, observation['status'], observation.get('http_status'), flush=True)
                    if access_restricted(observation, root):
                        stopped.add(case['source'])
                        observation['access_restriction_detected'] = True
                    if observation.get('api_status') in (401, 402, 429, 400, 422):
                        raise RuntimeError('API_GLOBAL_OR_REQUEST_ERROR_NO_RETRY')
                    save()
                if case['function_id'] == 'pdf_ingestion' and case['source'] not in stopped:
                    fc = result['engines']['firecrawl_cloud']
                    if fc['status'] == 'FETCHED':
                        fc['original_pdf'] = cloud_fetch(case, root, meter, client, original_pdf=True)
                        if access_restricted(fc['original_pdf'], root):
                            stopped.add(case['source'])
                save()
            run['status'] = 'COLLECTION_FINISHED_NOT_QUALITY_GRADED'
        except Exception as exc:
            run.update(status='STOPPED', error_type=type(exc).__name__,
                       error_category=str(exc) if re.fullmatch(r'[A-Z0-9_]+', str(exc)) else 'SEE_LEDGER')
        finally:
            for result in run['cases'].values():
                for observation in result['engines'].values():
                    if observation['status'] == 'PENDING':
                        observation['status'] = 'NOT_RUN_CAMPAIGN_STOPPED'
            if before is not None:
                try:
                    after = usage(client, root, meter, 'usage_after')
                    run['credits_balance_delta'] = before-after
                except Exception as exc:
                    run['usage_after_error_type'] = type(exc).__name__
            run.update(finished_at=now_iso(), client_requests=len(meter.data['requests']),
                       credits_reserved=meter.data['credits_reserved'])
            save()
    print(json.dumps({k: run.get(k) for k in ['campaign_id', 'status', 'client_requests',
                                            'credits_balance_delta', 'credits_reserved']}, indent=2))
    return int(run['status'] == 'STOPPED')


if __name__ == '__main__':
    raise SystemExit(main())
