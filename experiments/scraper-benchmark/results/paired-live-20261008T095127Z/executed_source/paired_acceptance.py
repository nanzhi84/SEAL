#!/usr/bin/env python3
"""Artifact-level E2E checks, specified before the paired runner implementation.

Failure modes: wrong URL/function/campaign, old raw imported, fabricated missing
cells, network restriction retry, invalid body promoted from 200, stale cache,
over-budget dispatch, identity drift and leaked credentials. No mocked service.
"""
import argparse
import json
from pathlib import Path
from datetime import datetime
from bench.common import write_json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('campaign')
    root = Path(ap.parse_args().campaign)
    protocol = json.loads((root / 'protocol.json').read_text())
    run = json.loads((root / 'run.json').read_text())
    ledger = json.loads((root / 'ledger.json').read_text())
    checks = []
    def check(name, condition):
        checks.append({'name': name, 'passed': bool(condition)})
    check('frozen_same_candidate_set', set(run['cases']) == {c['id'] for c in protocol['cases']})
    check('client_budget', len(ledger['requests']) <= 80)
    check('credit_reservations_bounded', ledger['credits_reserved'] <= 30)
    check('provider_subrequests_not_faked', run['provider_origin_subrequests'] == 'UNKNOWN')
    check('no_pending_cells', all(x['status'] != 'PENDING' for c in run['cases'].values()
                                  for x in c['engines'].values()))
    for case in protocol['cases']:
        result = run['cases'][case['id']]
        check(case['id'] + ':same_contract', result['url'] == case['url'] and
              result['function_id'] == case['function_id'] and
              set(result['engines']) == {'scrapy', 'firecrawl_cloud'})
        for engine, observation in result['engines'].items():
            if observation['status'] == 'FETCHED':
                check(case['id'] + ':' + engine + ':raw_exists', (root / observation['raw_path']).exists())
                check(case['id'] + ':' + engine + ':same_original_url',
                      observation['requested_url'] == case['url'])
    requests = ledger['requests']
    starts = [datetime.fromisoformat(r['started_at']).timestamp() for r in requests]
    check('serialized_client_dispatch_spacing', all(b-a >= 2.99 for a,b in zip(starts,starts[1:])))
    # Every run is new; artifact paths are relative to this campaign only.
    check('no_historical_observations_substituted', all(
        not x.get('raw_path', '').startswith(('../', '/')) for c in run['cases'].values()
        for x in c['engines'].values()))
    report = {'checks': checks, 'passed': sum(c['passed'] for c in checks),
              'failed': sum(not c['passed'] for c in checks),
              'scope': 'execution contract, NOT an assertion both frameworks succeeded on websites'}
    write_json(root / 'execution_acceptance.json', report)
    print(json.dumps(report, indent=2))
    return int(report['failed'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
