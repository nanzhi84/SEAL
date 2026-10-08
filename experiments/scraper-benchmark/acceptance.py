#!/usr/bin/env python3
"""E2E acceptance of CLI artifacts; --baseline grades inherited artifacts only.

No mocks, no unit tests, no network in this runner except child loopback HTTP.
Exit 1 means a tested observable contract failed; BLOCKED is never PASS.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
R = HERE / 'results'
checks = []


def load(path):
    return json.loads((HERE / path).read_text())


def check(name, condition, detail=None):
    checks.append({'name': name, 'status': 'PASS' if condition else 'FAIL', 'detail': detail})


def command(*args):
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith('proxy')}
    env['NO_PROXY'] = '127.0.0.1,localhost'
    proc = subprocess.run([sys.executable, *args], cwd=HERE, env=env,
                          capture_output=True, text=True, timeout=150)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    log = HERE / 'logs' / ('acceptance_' + stamp + '_' + '_'.join(args).replace('/', '_') + '.log')
    log.write_text(proc.stdout + proc.stderr)
    with (R / 'execution_log.jsonl').open('a') as f:
        f.write(json.dumps({'at': stamp, 'command': [sys.executable, *args],
                            'exit_code': proc.returncode, 'log': str(log.relative_to(HERE)),
                            'network_scope': 'loopback / offline / Firecrawl status only'}) + '\n')
    check('command:' + ' '.join(args), proc.returncode == 0, {'exit_code': proc.returncode,
                                                            'log': str(log.relative_to(HERE))})


def main():
    baseline = '--baseline' in sys.argv
    if not baseline:
        for args in [('scrapy_test.py', 'replay'), ('scrapy_test.py', 'pdf'),
                     ('fixture_test.py', 'raw'), ('fixture_test.py', 'playwright'),
                     ('list_replay.py',), ('quality_audit.py',), ('firecrawl_test.py', 'status')]:
            command(*args)
    root = 'results/parsed' if baseline else 'results/replay'
    expected = load('fixtures/expected.json')
    company = load(root + '/D_company.raw.json')['structured']
    for field, value in expected['company'].items():
        check('D:' + field, company.get(field) == value,
              {'expected': value, 'actual': company.get(field)})
    pdf = load('results/parsed/C_pdf.raw.pdfparse.json' if baseline
               else 'results/parsed/C_pdf.pdfparse.json')
    check('PDF:page_count', pdf['page_count'] == expected['pdf']['pages'])
    for p in pdf['pages']:
        a, b = p['char_span']
        check(f"PDF:page_{p['page']}_locatable", pdf['full_text'][a:b] == p['text'])
    if not baseline:
        replay = load('results/replay_check.json')
        check('Replay:deterministic_all', replay['checked'] == 14 and
              replay['deterministic'] == 14, replay)
        fixtures = load('results/fixtures_raw.json')
        docs = fixtures['versions']
        a, b, c, again = [docs[n] for n in ['a', 'b', 'c', 'a_repeat']]
        check('Revision:raw_A_B_differ', a['raw_hash'] != b['raw_hash'])
        check('Revision:content_A_B_equal', a['content_hash'] == b['content_hash'])
        check('Revision:content_B_C_differ', b['content_hash'] != c['content_hash'])
        check('Revision:repeat_equal', a['content_hash'] == again['content_hash'] and
              a['raw_hash'] == again['raw_hash'])
        check('Revision:identity_constant', len({d['identity'] for d in docs.values()}) == 1)
        check('Revision:two_revisions_four_observations',
              fixtures['revision_count'] == 2 and fixtures['observation_count'] == 4)
        check('Revision:title_change_detected', fixtures['title_change_detected'])
        check('Revision:legitimate_body_timestamp_preserved', fixtures['body_timestamp_preserved'])
        check('JSON:three_documents', len(fixtures['json']['documents']) == 3)
        check('JSON:attachment_relation', fixtures['json']['documents'][0]['attachments'] ==
              ['http://fixture.invalid/files/DOC-001.pdf'])
        check('TXT:field_evidence', fixtures['txt']['evidence_valid'])
        check('JS:raw_does_not_claim_rendered', fixtures['dynamic']['target_found'] is False)
        pw = load('results/fixtures_playwright.json')
        check('JS:rendered_target', pw['dynamic']['target_found'])
        check('iframe:rendered_target', pw['dynamic']['iframe_target_found'])
        check('Browser:no_external_requests', pw['external_requests'] == 0)
        check('Browser:subrequests_metered_and_paced', len(pw['browser_requests']) == 2 and
              all(b['monotonic'] - a['monotonic'] >= 3 for a, b in
                  zip(pw['local_requests'], pw['local_requests'][1:])))
        walked = load('results/list_replay_e2e.json')
        check('Discovery:real_response_callback_walk', walked['list_count'] == 2 and
              len(walked['details']) == 5 and len(walked['edges']) == 5 and
              walked['request_count'] == 7 and walked['all_bodies_match_archived'])
        quality = load('results/quality.json')
        check('A:two_page_coverage', quality['discovery']['matched'] == 40 and
              quality['discovery']['expected'] == 40)
        check('A:five_verified_parent_edges', quality['discovery']['parent_edges'] == 5)
        check('Details:seven_exact_bodies', sum(d['body_exact'] for d in quality['details']) == 7)
        check('Details:seven_titles_dates', all(d['title_exact'] and d['date_exact']
                                               for d in quality['details']))
        check('PDF:111_rows_independent', quality['pdf']['matched_rows'] == 111 and
              quality['pdf']['ordered'] and quality['pdf']['page_ranges_correct'])
        if not os.getenv('FIRECRAWL_API_KEY'):
            fc = load('results/firecrawl_status.json')
            check('Firecrawl:honest_missing_key', fc['status'] == 'BLOCKED' and
                  fc['external_requests_this_run'] == 0 and fc['credits_this_run'] == 0)
    report = {'at': datetime.now(timezone.utc).isoformat(), 'baseline': baseline,
              'command': ' '.join(sys.argv), 'checks': checks,
              'passed': sum(c['status'] == 'PASS' for c in checks),
              'failed': sum(c['status'] == 'FAIL' for c in checks),
              'scope': 'local E2E + archived real response replay; not new live-site success'}
    (R / ('acceptance_baseline.json' if baseline else 'acceptance.json')).write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report['failed'] > 0)


if __name__ == '__main__':
    sys.exit(main())
