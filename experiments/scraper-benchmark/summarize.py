#!/usr/bin/env python3
"""Regenerate metrics / consolidated run log / integrity manifest, offline."""
import ast
import json
import statistics
from pathlib import Path

from bench.common import HERE, RESULTS, now_iso, read_json, sha256_hex, write_json


def j(name):
    return read_json(RESULTS / name)


def jsonlines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def code_counts():
    files = {}
    for path in sorted(HERE.rglob('*.py')):
        if '.venv' in path.parts or '__pycache__' in path.parts or 'results' in path.relative_to(HERE).parts:
            continue
        lines = path.read_text().splitlines()
        files[str(path.relative_to(HERE))] = {
            'physical_lines': len(lines),
            'nonblank_noncomment_lines': sum(bool(s.strip()) and not s.lstrip().startswith('#') for s in lines)}
    recipe = HERE / 'bench/recipes.py'
    functions = {node.name: node.end_lineno - node.lineno + 1
                 for node in ast.parse(recipe.read_text()).body if isinstance(node, ast.FunctionDef)}
    return {'files': files, 'recipe_function_physical_lines': functions,
            'note': 'actual code size, not development hours; shared harness != per-source cost'}


def source_matrix():
    same_fc = {'status': 'NOT_TESTED_IN_HISTORICAL_ROUND', 'reason': 'historical round only; current same-URL results are in paired_url_quality',
               'historical_status': 'BLOCKED due to missing environment key; now credential validated'}
    cases = {
        'A': ('PASS_ARCHIVED_SAMPLE', '2 lists / 5 details; 40/40 links on these pages only'),
        'B': ('PARTIAL', 'home + 1 news article; procurement notices NOT_TESTED; historical 403/UA retry tainted'),
        'C': ('PASS_ARCHIVED_SAMPLE', '107131 bytes; 5 pages; 111/111 independently checked table rows'),
        'D': ('BLOCKED_POLICY', 'robots transport failed; offline 9/9 fields after Recipe repair; search NOT_TESTED'),
        'E': ('PARTIAL', '200 shell, empty body, 2 iframe URLs; child documents not fetched'),
        'F': ('ACCESS_RESTRICTED', 'HTTP 412 in urllib preflight, not Scrapy parser failure'),
        'G': ('ACCESS_RESTRICTED', 'HTTP 412 in urllib preflight, not Scrapy parser failure'),
        'H': ('PASS_ARCHIVED_SAMPLE', '1 list / 1 batch document; 30 links not independently graded'),
    }
    return {key: {'scrapy': {'status': value[0], 'detail': value[1],
                             'evidence_source': 'inherited live artifacts; F/G preflight only'},
                  'firecrawl': same_fc,
                  'scrapy_playwright': {'status': 'FAILED_TAINTED' if key in 'FG' else 'NOT_TESTED',
                                       'detail': 'prior restricted-site attempts excluded; no replay as real-site success'}}
            for key, value in cases.items()}


def main():
    quality = j('quality.json')
    cloud_path = RESULTS / 'firecrawl_fixture-20261008-approved1'
    cloud = read_json(cloud_path / 'acceptance.json') if (cloud_path / 'acceptance.json').exists() else None
    paired_root = (RESULTS / j('current_paired_campaign.json')['campaign_id']
                   if (RESULTS / 'current_paired_campaign.json').exists() else None)
    paired = read_json(paired_root / 'quality.json') if paired_root and (paired_root / 'quality.json').exists() else None
    raw_stats = j('scrapy_stats_scrapy_raw.json')['scrapy_stats']
    events = jsonlines(RESULTS / 'run_log.jsonl')
    requests = [r for r in events if r['kind'] == 'request']
    latency = [r['elapsed_s'] for r in requests if r.get('elapsed_s') is not None]
    prior = j('prior_requests.json')
    fixtures = j('fixtures_raw.json')
    browser = j('fixtures_playwright.json')
    recipe_counts = code_counts()
    core = {group: sum(recipe_counts['recipe_function_physical_lines'][n] for n in names)
            for group, names in {
                'A': ['court_list', 'court_detail'], 'B': ['ccgp_list', 'ccgp_detail'],
                'D': ['companies_house_search', 'companies_house_company'],
                'E': ['stats_iframe_page'], 'H': ['spp_list', 'spp_detail']}.items()}
    metrics = {
        'generated_at': now_iso(), 'environment': j('environment.json'),
        'scope': 'partial same-URL current content comparison + separately labelled historical capability evidence',
        'paired_url_quality': paired,
        'comparison_validity': j('paired_audit.json') if (RESULTS / 'paired_audit.json').exists() else None,
        'framework_preference': None,  # Experimental comparison, not the user's engineering decision.
        'user_selection': read_json(HERE / 'selection_decision.json') if (HERE / 'selection_decision.json').exists() else None,
        'selection_recommendation': 'NO_OVERALL_WINNER_FROM_PARTIAL_COHORT',
        'credential_update': j('credential_update.json') if (RESULTS / 'credential_update.json').exists() else None,
        'historical_sites': source_matrix(), 'historical_offline_quality': quality,
        'scrapy_historical_live': {
            'requests': len(requests), 'http_200': sum(r['status'] == 200 for r in requests),
            'quality_note': '15 HTTP responses != 15 successful documents',
            'wall_seconds': raw_stats['elapsed_time_seconds'],
            'response_bytes_scrapy_stat': raw_stats['downloader/response_bytes'],
            'saved_response_body_bytes': sum(r['bytes'] for r in requests),
            'download_latency_median_seconds': statistics.median(latency),
            'download_latency_range_seconds': [min(latency), max(latency)],
            'peak_rss_bytes_scrapy_sampled': raw_stats['memusage/max'],
            'timing_caveat': 'single mixed-site run, scheduling + delay included, historical policy caveats; no speed ranking'},
        'budget': {
            'maximum_external_requests': 80, 'maximum_firecrawl_credits': 30,
            'historical_declared_top_level_requests': prior['total_requests'] + len(requests),
            'actual_total_external_requests': None,
            'unknown_reason': 'browser subrequests / redirect hops not fully logged; two historical API probes unsubstantiated',
            'earlier_synthetic_cloud_campaign': {'api_calls': cloud['request_count'] if cloud else 0,
                'credits': cloud['cost']['credit_balance_delta'] if cloud else 0,
                'target_site_requests': 0, 'approved_api_cap': 6, 'approved_credit_cap': 4},
            'approved_paired_campaign': {'client_http_api_cap': 80, 'credit_cap': 30,
                'client_requests': paired['client_requests'] if paired else 0,
                'source_client_gets_including_robots': (paired['requests_by_engine']['robots'] +
                    paired['requests_by_engine']['scrapy']) if paired else 0,
                'firecrawl_api_requests': paired['requests_by_engine']['firecrawl_cloud'] if paired else 0,
                'actual_credit_balance_delta': paired['credits_actual_balance_delta'] if paired else 0,
                'provider_origin_subrequests': 'UNKNOWN_BY_APPROVED_BUDGET_SCOPE',
                'new_campaign_scope_compliance': 'WITHIN_CLIENT_API_AND_CREDIT_CAPS' if paired else 'NOT_RUN'},
            'historical_actual_firecrawl_credits': None,
            'historic_declared_credits': prior['total_credits'],
            'cash_payment_initiated_this_continuation': 0,
            'whole_experiment_cash_cost': 'UNKNOWN; no billing evidence / no machine or bandwidth dollar metering',
            'historical_strict_origin_budget_compliance': 'UNKNOWN; old uncontrolled campaign is still locked',
            'research_traffic': {'context7_resolve_calls': 2, 'context7_query_calls': 3,
                                 'official_docs_fetch_urls': 3, 'additional_paired_round_context7_queries': 1,
                                 'note': 'research/control-plane calls excluded from target benchmark; provider fanout unknown'}},
        'local_e2e': {
            'raw_fixture_seconds': fixtures['elapsed_s'], 'raw_fixture_requests': len(fixtures['local_requests']),
            'playwright_fixture_seconds': browser['elapsed_s'],
            'playwright_fixture_requests': len(browser['local_requests']),
            'playwright_external_requests': browser['external_requests'],
            'raw_runner_peak_rss_bytes': fixtures['runner_peak_rss_bytes_macos'],
            'playwright_runner_peak_rss_bytes': browser['runner_peak_rss_bytes_macos'],
            'memory_scope': 'Python runner; browser process tree not measured',
            'list_replay': j('list_replay_e2e.json'), 'acceptance': j('acceptance.json')},
        'revision': {'raw_hashes': {k: d['raw_hash'] for k, d in fixtures['versions'].items()},
                     'content_hashes': {k: d['content_hash'] for k, d in fixtures['versions'].items()},
                     'observations': fixtures['observation_count'], 'revisions': fixtures['revision_count'],
                     'added_by': 'SEAL experiment layer, not Scrapy/Firecrawl'},
        'replay': j('replay_check.json'), 'pdf_detail': j('pdf_detail.json'),
        'firecrawl': {'historical_environment_check': j('firecrawl_status.json'),
                      'approved_fixture_campaign': cloud,
                      'approved_paired_url_campaign': paired}, 'code_size': recipe_counts,
        'per_source_recipe_lines': core,
        'not_tested': ['same-URL legal article/PDF quality; Firecrawl self-host; filename-controlled A/B Parse retest',
                       'real procurement notice (B stopped at 403), attachment download, actual Companies House search',
                       'live discovery-driven scheduler, real-site Playwright group, authorized JSON API, PDF scans/OCR',
                       'document disappearance, complete scheduled revision workflow, Agent isolation enforcement'],
    }
    write_json(RESULTS / 'metrics.json', metrics)
    run_log = {
        'generated_at': now_iso(), 'inherited_declared_batches': prior['items'],
        'latest_pairing_audit': j('paired_audit.json') if (RESULTS / 'paired_audit.json').exists() else None,
        'approved_paired_campaign': {'run': read_json(paired_root / 'run.json'),
                                     'ledger': read_json(paired_root / 'ledger.json'),
                                     'quality': paired} if paired else None,
        'inherited_instrumented_scrapy_requests': requests,
        'inherited_excluded_run': jsonlines(RESULTS / 'run_log_run1_impolite.jsonl'),
        'continuation_commands': jsonlines(RESULTS / 'execution_log.jsonl'),
        'blockers': [j('firecrawl_status.json'), j('network_blocked.json')],
        'blocked_command_logs': ['logs/firecrawl_fixtures_blocked.log', 'logs/scrapy_live_blocked.log'],
        'approved_cloud_campaign': {
            'path': str(cloud_path.relative_to(HERE)),
            'http_requests': read_json(cloud_path / 'http_ledger.json'),
            'sdk_calls': read_json(cloud_path / 'ledger.json'),
            'cost': read_json(cloud_path / 'cost.json'),
            'acceptance': cloud,
        } if cloud else None,
        'credential_update': j('credential_update.json') if (RESULTS / 'credential_update.json').exists() else None,
        'limitations': ['requests are logged at response time in legacy runs, not reliable dispatch metering',
                        'logs/run_log_scrapy_raw.jsonl duplicates results/run_log.jsonl; do NOT double count',
                        'early browser stats and later browser log describe different attempts',
                        'first local browser run was unpaced (loopback only); preserved under fixtures_playwright_unpaced_local.json'],
    }
    write_json(RESULTS / 'run_log.json', run_log)
    manifest = {}
    for folder in ['fixtures', 'results/raw', 'results/replay', 'results/parsed',
                   'results/firecrawl_fixture-20261008-approved1',
                   *([str(paired_root.relative_to(HERE))] if paired_root else [])]:
        for path in sorted((HERE / folder).rglob('*')):
            if path.is_file():
                manifest[str(path.relative_to(HERE))] = {'bytes': path.stat().st_size,
                                                        'sha256': sha256_hex(path.read_bytes())}
    for path in [HERE / 'paired_protocol.json', HERE / 'selection_decision.json', RESULTS / 'paired_audit.json']:
        if path.exists():
            manifest[str(path.relative_to(HERE))] = {'bytes': path.stat().st_size,
                                                    'sha256': sha256_hex(path.read_bytes())}
    write_json(RESULTS / 'artifact_manifest.json', manifest)
    print(json.dumps({'metrics': 'results/metrics.json', 'run_log': 'results/run_log.json',
                      'hashed_artifacts': len(manifest), 'per_source_recipe_lines': core,
                      'live_scrapy': metrics['scrapy_historical_live'],
                      'local_raw_s': fixtures['elapsed_s'], 'local_browser_s': browser['elapsed_s'],
                      'acceptance': [metrics['local_e2e']['acceptance']['passed'],
                                     metrics['local_e2e']['acceptance']['failed']]}, indent=2))


if __name__ == '__main__':
    main()
