"""Repeatable final checks over actual CLI/SQL/S3 evidence from Issue20 E2E.

No service or SDK result is fabricated. The native envelopes were copied from
the isolated S3 API, and each filename is the verified CAS content key.
Run: uv run --no-sync python tests/issue20_supplement.py artifacts/<issue20-run>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from crawl4ai import CrawlResult
from issue20_lab import ROOT, digest, write


def main():
    artifact = Path(sys.argv[1]).resolve()
    checks = []

    def check(label, success, **detail):
        checks.append(dict(assertion=label, status='PASS' if success else 'FAIL', **detail))
        print(checks[-1]['status'], label)

    summary = json.loads((artifact / 'summary.json').read_text())
    check('source E2E completed its actual CLI/SQL/S3 chain without failing assertions',
          summary['passed'] > 0 and summary['failed'] == 0)
    envelopes = list(artifact.glob('result-*.json'))
    native_fields = set(CrawlResult.model_fields)
    missing = {path.name: sorted(native_fields - set(json.loads(path.read_text())['result']))
               for path in envelopes if native_fields - set(json.loads(path.read_text())['result'])}
    check('actual stored result envelopes contain every native SDK CrawlResult field',
          bool(envelopes) and not missing, result_objects=len(envelopes),
          sdk_fields=len(native_fields), missing=missing)
    check('stored complete JSON evidence exactly matches actual verified S3 CAS keys',
          all(digest(path.read_bytes()) == path.stem[7:] for path in envelopes))
    receipts = list(artifact.glob('*-receipt.json'))
    bad_receipts = [path.name for path in receipts if any(name in path.read_text()
                    for name in ('manifest_id', 'manifest_ref', 'manifest_hash'))]
    check('all actual CLI receipts omit former manifest_id and other object manifest references',
          bool(receipts) and not bad_receipts, receipt_files=len(receipts), violations=bad_receipts)
    sql = json.loads((artifact / 'sql.json').read_text())
    bad_runs = [run['id'] for run in sql['run'] if any(name in json.dumps(run)
                for name in ('manifest_id', 'manifest_ref', 'manifest_hash'))]
    check('actual stored Run rows omit former manifest_id and other object manifest references',
          bool(sql['run']) and not bad_runs, violations=bad_runs)
    metadata = json.loads((artifact / 'environment.json').read_text())
    check('supplement checks the same product bytes as the completed source E2E',
          all((ROOT / path).exists() and digest((ROOT / path).read_bytes()) == sha
              for path, sha in metadata['source_sha256'].items()))
    check('all original E2E executable test files remain byte identical',
          all((ROOT / path).exists() and digest((ROOT / path).read_bytes()) == sha
              for path, sha in metadata['test_sha256'].items()))
    output = dict(command=f'uv run --no-sync python tests/issue20_supplement.py {artifact}',
                  source_artifact=str(artifact), supplement_sha256=digest(Path(__file__).read_bytes()),
                  passed=sum(c['status'] == 'PASS' for c in checks),
                  failed=sum(c['status'] == 'FAIL' for c in checks), assertions=checks)
    write(artifact / 'supplement.json', output)
    return int(output['failed'] != 0)


if __name__ == '__main__':
    raise SystemExit(main())
