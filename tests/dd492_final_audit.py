"""Independent final audit; run after bounded single-URL diagnosis and reconciliation.
uv run --no-sync python tests/dd492_final_audit.py
Uses existing isolated database and frozen acquisition receipts; no bulk downloads.
"""
import json
import sys
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from dd492_pdf_acceptance import Acceptance, sha

x = Acceptance('20261014_160902')
x.audit_only = True
x.live_executed = True
old_path = x.runner_out.parent / 'retry-20261014_174213/reparse.original-versions.json'
old = json.loads(old_path.read_text())
current = {r['id']: json.loads(json.dumps(r, default=str)) for r in x.versions()}
x.check('all pre-correction immutable Versions remain unchanged', all(current.get(r['id']) == r for r in old), historical_versions=len(old))
with psycopg.connect(x.env['SEAL_DATABASE_URL'], row_factory=dict_row) as c:
    versions = c.execute('SELECT * FROM seal.current_resource_version').fetchall()
    source = c.execute("SELECT config FROM seal.source WHERE id='dd-492'").fetchone()
    counts = {t: c.execute('SELECT count(*) AS n FROM seal.' + t).fetchone()['n'] for t in ['run','observation','version']}
x.check('Source entry is restored to official portal', source['config']['entry_urls'] == ['https://vanban.chinhphu.vn/'])
x.dump('current-versions.json', versions)
x.check('all 299 previously successful PDFs have a current Version', sum('.pdf' in v['url'].lower() for v in versions) == 299, count=len(versions))
for v in versions:
    if '.pdf' not in v['url'].lower():
        continue
    env = x.envelope(v)
    metadata = env.get('result', {}).get('metadata') or {}
    x.check('current Version uses full verbatim text contract', metadata.get('pdf_text_contract') == 'verbatim_v1', url=v['url'], version_id=v['id'])
x.dump('database-before-resume.json', counts)
# Full independent native-text comparison on every page is in the reusable audit.
frozen_before = {str(p): sha(p.read_bytes()) for p in x.runner_out.glob('page-*.json')}
x.live()
frozen_after = {str(p): sha(p.read_bytes()) for p in x.runner_out.glob('page-*.json')}
x.check('ordinary resume preserves frozen list receipts', frozen_before == frozen_after)
with psycopg.connect(x.env['SEAL_DATABASE_URL'], row_factory=dict_row) as c:
    after = {t: c.execute('SELECT count(*) AS n FROM seal.' + t).fetchone()['n'] for t in counts}
x.check('ordinary resume adds zero Runs Observations and Versions', counts == after, before=counts, after=after)
crosscheck = x.runner_out.parent / 'retry-20261014_175707/no-text-poppler-crosscheck.json'
no_text = json.loads(crosscheck.read_text())
x.check('independent Poppler no-text crosscheck has 240 and no substantive candidates', no_text['count'] == 240 and not no_text['substantive_candidates'], evidence=str(crosscheck), evidence_sha256=sha(crosscheck.read_bytes()))
failed = x.finish()
print('FINAL_ARTIFACT=' + str(x.out))
sys.exit(failed)
