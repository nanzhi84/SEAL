"""Combine immutable full audit and bounded delta proofs; never fetch or reparse."""
import json
import sys
from pathlib import Path
from dd492_pdf_acceptance import ROOT,sha

base=ROOT/'artifacts/dd-492-pdf-acceptance/20261014_160902'
components={
'539_original_full_audit':base/'retry-20261015_102627',
'9_new_original_delta':base/'retry-20261015_103835',
'2_large_original_delta':base/'retry-20261015_105514',
'final_current_and_coverage':base/'retry-20261015_105605',
'final_network_guarded_resume':base/'retry-20261015_105614',
'2001_page_fixture':ROOT/'artifacts/dd-492-pdf-acceptance/20261014_174232',
'64MiB_collect_snapshot_api':ROOT/'artifacts/dd-492-pdf-acceptance/20261015_104251',
'explicit_timeout_green':ROOT/'artifacts/dd-492-pdf-acceptance/20261015_105108',
'real_same_contract_recheck':base/'retry-20261015_102448'}
proofs={}
for name,p in components.items():
    a=json.loads((p/'assertions.json').read_text())
    failures=[r for r in a if r['status']=='FAIL']
    allowed={'all PDF attempts succeed or explicitly require a text layer','all eleven originals acquired and archived'} if name in ('539_original_full_audit','9_new_original_delta') else set()
    assert all(r['name'] in allowed for r in failures),(name,failures)
    proofs[name]={'artifact':str(p),'pass':sum(r['status']=='PASS' for r in a),'fail':len(failures),'historical_coverage_failures_resolved_by_final_550_coverage':bool(failures),'assertions_sha256':sha((p/'assertions.json').read_bytes())}
counts=json.loads((components['final_current_and_coverage']/'final-counts.json').read_text())
assert counts=={'selected':550,'originals':550,'parsed':299,'no_body_text':251,'network_failed':0}
report={'status':'PASS','scope':'Only ten selected real listing pages; not full history','counts':counts,'components':proofs,
'acceptance_contract':'All550 original PDF bytes archived;299 full original text-layer results;251 explicit no-body-text errors without OCR or successful Versions',
'evidence':{'list_direct_pdf_links':str(base/'list-direct-pdf-audit.json'),'original240_no_text_crosscheck':str(base/'retry-20261014_175707/no-text-poppler-crosscheck.json'),'new9_no_text_crosscheck':str(base/'retry-20261015_103835/new-originals-poppler-crosscheck.json'),'large2_no_text_crosscheck':str(base/'retry-20261015_105514/new-originals-poppler-crosscheck.json'),'timeout_red':str(ROOT/'artifacts/dd-492-pdf-acceptance/20261015_104950')},
'environment':'Independent isolated PostgreSQL databases; credentials privately from .env.local; S3 API only; test work/artifacts local; no OCR',
'commands':['uv run --no-sync python tests/dd492_final_audit.py','uv run --no-sync python tests/dd492_gap_audit.py <diagnostic-artifact> <observations-file>','uv run --no-sync python tests/dd492_large_pdf_e2e.py','uv run --no-sync python tests/dd492_timeout_e2e.py','uv run --no-sync python tests/dd492_delivery_audit.py','uv run --no-sync python tests/dd492_resume_audit.py','uv run --no-sync python tests/dd492_combined_report.py'],
'budget_history':'Ten-page baseline frozen50MiB/60s; finite gap diagnostics300s; two originals required128MiB plus correction of SDK implicit30s read ceiling. Final delivery Source128MiB/300s; ordinary existing-state resume uses frozen50MiB/60s config.',
'limits':['251 image/scan originals have no legal body text layer; signatures and page numbers are not body text','Invalid original UTF16 symbols remain escaped with explicit metadata warning; semantic symbol reconstruction is unknown','No full-history or production deployment claim','No original was redownloaded merely to repeat an already passed full audit']}
p=base/'final-acceptance-report.json';p.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(p);print(json.dumps({'status':'PASS','counts':counts,'components':proofs},ensure_ascii=False))
