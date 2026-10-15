"""Final delta reconciliation: baseline proof plus new original proof, no reparse."""
import json
import sys
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from dd492_pdf_acceptance import Acceptance

x=Acceptance('20261014_160902')
base=x.runner_out.parent/'retry-20261015_102627'
base_current=json.loads((base/'current-versions.json').read_text())
with psycopg.connect(x.env['SEAL_DATABASE_URL'],row_factory=dict_row) as c:
    current=c.execute('SELECT * FROM seal.current_resource_version').fetchall()
    source=c.execute("SELECT config FROM seal.source WHERE id='dd-492'").fetchone()
now={r['url']:r for r in current}
x.check('all baseline successful current Version IDs and original hashes unchanged',all(now.get(r['url'],{}).get('id')==r['id'] and now[r['url']]['body_hash']==r['body_hash'] and now[r['url']]['result_ref']==r['result_ref'] for r in base_current),baseline=len(base_current))
old=json.loads((x.runner_out.parent/'retry-20261014_174213/reparse.original-versions.json').read_text())
versions={r['id']:json.loads(json.dumps(r,default=str)) for r in x.versions()}
x.check('all immutable historical Versions remain unchanged after bounded gap diagnosis',all(versions.get(r['id'])==r for r in old),historical=len(old))
x.check('final Source is official portal',source['config']['entry_urls']==['https://vanban.chinhphu.vn/'])
rows={r['url']:r for r in x.rows()}
manifest=json.loads((x.runner_out/'pdf-manifest.json').read_text())
selected=[rows[r['url']] for r in manifest]
x.check('all 550 selected PDFs have archived originals',len(selected)==550 and all(r['archive_ref'] for r in selected),missing=[r['url'] for r in selected if not r['archive_ref']])
x.check('all selected PDFs parse or explicitly lack body text',all(r['outcome'] in ('parsed','unchanged') or r['error']=='pdf_text_layer_required' for r in selected))
heads=json.loads((x.runner_out.parent/'large-pdf-head.json').read_text())
for h in heads:
    row=rows[h['url']]
    size=x.s3.head_object(Bucket=x.env['SEAL_S3_BUCKET'],Key=row['archive_ref'])['ContentLength']
    x.check('large archived object length matches official HEAD full original',size==int(h['content_length']),url=h['url'],bytes=size)
x.dump('final-selected-observations.json',selected)
x.dump('final-current-versions.json',current)
x.dump('final-counts.json',{'selected':len(selected),'originals':sum(bool(r['archive_ref']) for r in selected),'parsed':sum(r['outcome'] in ('parsed','unchanged') for r in selected),'no_body_text':sum(r['error']=='pdf_text_layer_required' for r in selected),'network_failed':sum(r['outcome']=='network_failed' for r in selected)})
failed=x.finish()
print('DELIVERY_ARTIFACT='+str(x.out))
sys.exit(failed)
