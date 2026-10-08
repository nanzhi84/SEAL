#!/usr/bin/env python3
"""Zero-network protocol validation and comparability audit.

This is NOT a paired live crawler and creates no simulated fetch results.
Historical independent capability checks cannot become matched observations by
having similar titles, parsers, or hashes. Only real same-campaign URL Scrape
records with identical function/scenario/oracle contracts may be compared.
"""
import json
from collections import Counter
from pathlib import Path

from bench.common import HERE, RESULTS, now_iso, read_json, sha256_hex, write_json


def main():
    protocol_path = HERE / 'paired_protocol.json'
    protocol = read_json(protocol_path)
    cases = protocol['cases']
    checks = []

    def check(name, condition):
        checks.append({'name': name, 'passed': bool(condition)})

    check('one_shared_manifest_for_exactly_two_primary_engines',
          protocol['engines'] == ['scrapy', 'firecrawl_cloud'])
    check('case_ids_unique', len({c['id'] for c in cases}) == len(cases))
    check('url_function_pairs_unique', len({(c['url'], c['function_id']) for c in cases}) == len(cases))
    check('contracts_defined_for_every_case', all(c['function_id'] in protocol['function_contracts'] for c in cases))
    details = Counter(c['source'] for c in cases if c['role'] == 'detail')
    check('at_most_five_details_per_source', max(details.values()) <= 5)
    check('all_original_candidate_origins_remain', {c['source'] for c in cases} == {'A', 'B', 'C', 'D', 'E', 'H'})
    check('F_G_jointly_excluded', {c['source'] for c in protocol['excluded']} == {'F', 'G'})
    notice = next(c for c in cases if c['id'] == 'B_notice_1')
    check('B_is_actual_procurement_path_not_news', '/cggg/' in notice['url'] and '/news/' not in notice['url'])
    check('B_official_navigation_evidence_exists', notice['url'] in
          (RESULTS / 'raw/B_home.raw.html').read_text())
    iframe = next(c for c in cases if c['id'] == 'E_iframe')
    check('E_iframe_from_actual_source_html', iframe['url'] in
          (RESULTS / 'raw/E_list.raw.html').read_text())
    check('no_unapproved_cloud_dispatch', protocol['execution_gate']['cloud_calls_allowed_until_scope_resolved'] is False)
    check('no_caching_no_retry', protocol['controls']['retries'] == 0 and
          'max_age=0' in protocol['controls']['client_cache'])

    historical_scrapy = []
    for path in sorted((RESULTS / 'parsed').glob('*.raw.json')):
        data = read_json(path)
        url = data.get('source_url') or data.get('url')
        if url:
            historical_scrapy.append({'url': url, 'artifact': str(path.relative_to(HERE)),
                                       'campaign': 'historical_scrapy_live',
                                       'not_eligible_for_new_round': True})
    cloud_root = RESULTS / 'firecrawl_fixture-20261008-approved1'
    historical_cloud = []
    for path in sorted(cloud_root.glob('fixture_*.json')):
        if path.name == 'fixture_comparison.json':
            continue
        data = read_json(path)
        historical_cloud.append({'url': (data.get('metadata') or {}).get('url'),
                                  'artifact': str(path.relative_to(HERE)),
                                  'operation': 'PARSE_UPLOAD_SYNTHETIC_NOT_URL_SCRAPE'})
    shared_urls = {d['url'] for d in historical_scrapy} & {d['url'] for d in historical_cloud}
    check('historical_sources_have_zero_same_url_pairs', not shared_urls)

    current = RESULTS / 'current_paired_campaign.json'
    quality = None
    if current.exists():
        campaign = RESULTS / read_json(current)['campaign_id']
        quality = read_json(campaign / 'quality.json') if (campaign / 'quality.json').exists() else None
    matrix = []
    for case in cases:
        for scenario in protocol['scenarios']:
            matrix.append({
                'case_id': case['id'], 'url': case['url'], 'source': case['source'],
                'function_id': case['function_id'], 'scenario': scenario,
                'required_fields': protocol['function_contracts'][case['function_id']],
                'scrapy': {'status': 'NOT_RUN', 'new_live_observation': None},
                'firecrawl_cloud': {'status': 'NOT_RUN', 'new_live_observation': None},
                'eligible_pair': False, 'reason': 'EXECUTION_GATE_NOT_RESOLVED; old artifacts not substituted',
            })
    if quality:
        matrix = quality['matrix']
    result = {
        'at': now_iso(), 'audit_command': '.venv/bin/python paired_audit.py',
        'protocol_sha256': sha256_hex(protocol_path.read_bytes()),
        'protocol_checks': checks, 'checks_passed': sum(c['passed'] for c in checks),
        'checks_failed': sum(not c['passed'] for c in checks),
        'cohort_urls': len(cases), 'cohort_function_scenario_pairs': len(cases) * len(protocol['scenarios']),
        'historical_scrapy_observations': historical_scrapy,
        'historical_cloud_uploads': historical_cloud,
        'historical_same_url_pair_count': len(shared_urls),
        'same_url_live_operation_pairs': quality['same_url_successful_operation_pairs'] if quality else 0,
        'snapshot_content_quality_pairs': len(quality['snapshot_quality_pairs']) if quality else 0,
        'strict_freshness_verified_pairs': quality['strict_freshness_verified_pairs'] if quality else 0,
        'comparison_status': quality['status'] if quality else 'NO_VALID_PAIRED_LIVE_EXPERIMENT',
        'completed_campaign': str(campaign.relative_to(HERE)) if quality else None,
        'framework_preference': None,
        'recommendation': 'No overall winner. Partial same-URL content evidence now exists; do not infer strict freshness or whole-suite superiority.',
        'matrix': matrix, 'excluded_sources': protocol['excluded'],
        'new_source_requests': quality['requests_by_engine']['robots'] + quality['requests_by_engine']['scrapy'] if quality else 0,
        'new_firecrawl_api_calls': quality['requests_by_engine']['firecrawl_cloud'] if quality else 0,
        'new_credits': quality['credits_actual_balance_delta'] if quality else 0,
        'next_gate': protocol['execution_gate'],
    }
    write_json(RESULTS / 'paired_audit.json', result)
    print(json.dumps({key: result[key] for key in [
        'checks_passed', 'checks_failed', 'cohort_urls', 'cohort_function_scenario_pairs',
        'historical_same_url_pair_count', 'same_url_live_operation_pairs',
        'snapshot_content_quality_pairs', 'strict_freshness_verified_pairs', 'comparison_status',
        'new_source_requests', 'new_firecrawl_api_calls', 'new_credits']}, indent=2))
    return int(result['checks_failed'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
