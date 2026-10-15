"""Summarize independently recorded assertions, retain nonzero coverage failures."""
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
a = json.loads((out / 'assertions.json').read_text())
failures = [r for r in a if r['status'] == 'FAIL']
coverage = [r for r in failures if r['name'] == 'all PDF attempts succeed or explicitly require a text layer']
integrity = [r for r in failures if r not in coverage]
pages = [json.loads(p.read_text()) for p in out.glob('real-pdf-*.text-evidence.json')]
rows = json.loads((out/'live.observations.json').read_text())
report = {
    'status': 'FAIL' if failures else 'PASS',
    'scope': 'Ten selected real listing pages only; not full history',
    'assertions': {'pass':len(a)-len(failures), 'fail':len(failures)},
    'integrity_failures':integrity,
    'coverage_failures':coverage,
    'selected_pdfs':len(rows),
    'originals_archived':sum(bool(r['body_hash']) for r in rows),
    'parsed_or_unchanged':sum(r['outcome'] in ('parsed','unchanged') for r in rows),
    'no_body_text':sum(r['error']=='pdf_text_layer_required' for r in rows),
    'network_failures':sum(r['outcome']=='network_failed' for r in rows),
    'audited_original_page_count':sum(p['pages'] for p in pages),
    'audit_command':'uv run --no-sync python tests/dd492_final_audit.py',
    'report_command':f'uv run --no-sync python tests/dd492_audit_report.py {out}',
    'environment': 'Private .env.local; isolated seal_dd492_acceptance_20261014_160902; S3 API only; existing frozen acquisition receipts; no OCR',
    'limitations': ['11 download failures have no original bytes to validate', 'Existing acquisition records and original hashes were audited; no bulk refetch of moving public source', 'Broken original UTF16 units are escaped with explicit warning; unknown symbol meanings are not recovered'],
}
(out/'final-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False))
sys.exit(bool(failures))
