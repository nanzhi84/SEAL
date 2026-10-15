"""One changed-budget diagnostic of exactly the original eleven missing originals.
Run once after fixed baseline audit; never run this as an automatic retry loop.
"""
import io
import json
import re
import sys
import yaml
from lxml import html
from pypdf import PdfReader
from dd492_pdf_acceptance import Acceptance, preserve_utf16_units, sha

x=Acceptance('20261014_160902')
before=x.rows()
latest={r['url']:r for r in before}
urls=sorted(u for u,r in latest.items() if r['outcome']=='network_failed')
x.check('only original eleven missing originals are selected', len(urls)==11, urls=urls)
if len(urls)!=11:
    sys.exit(x.finish())
config=yaml.safe_load(open('sources/dd-492.yaml'))
config['entry_urls']=urls
config['concurrency']=2
config['crawl4ai'].update(max_depth=0,page_timeout_ms=300000)
p=x.work/'eleven-gap-long-timeout.yaml'
p.write_text(yaml.safe_dump(config))
x.dump('eleven-gap-config.json',config)
x.dump('eleven-gap-before.json',before)
x.dump('eleven-gap-versions-before.json',x.versions())
x.command('eleven-gap-long-timeout',['seal','collect',str(p),'--no-progress'],timeout=3700)
ids={r['id'] for r in before}
rows=[r for r in x.rows() if r['id'] not in ids]
x.check('bounded diagnosis observes exactly eleven missing URLs once',len(rows)==11 and {r['url'] for r in rows}==set(urls),urls=[r['url'] for r in rows])
x.dump('eleven-gap-after.json',rows)
for row in rows:
    u=row['url']
    if not row['archive_ref']:
        x.check('still unavailable original remains explicit failure',row['outcome'] in ('network_failed','parse_failed') and bool(row['error']),url=u,error=row['error'])
        continue
    body=x.object(row['body_hash'])
    x.check('new original hash and archive references agree',sha(body)==row['body_hash']==row['archive_ref'] and row['body_hash'] in row['attachment_refs'],url=u)
    reader=PdfReader(io.BytesIO(body))
    env=x.envelope(row)
    result=env.get('result',{})
    if row['sequence'] is None:
        x.check('new no-text result is explicit without Version',row['outcome']=='parse_failed' and row['error']=='pdf_text_layer_required',url=u)
        continue
    nodes=html.fromstring(result.get('cleaned_html') or '<div/>').xpath('//*[@data-page]')
    x.check('new successful original retains every page in order', [int(n.attrib['data-page']) for n in nodes]==list(range(1,len(reader.pages)+1)),url=u,pages=len(reader.pages))
    missing=[]
    for i,page in enumerate(reader.pages):
        native,_=preserve_utf16_units(page.extract_text() or '')
        actual=nodes[i].text_content() if i<len(nodes) else ''
        if re.sub(r'\s+','',native)!=re.sub(r'\s+','',actual):missing.append(i+1)
    metadata=result.get('metadata') or {}
    x.check('new successful text preserves all original numbers punctuation and pages',not missing and metadata.get('pages')==len(reader.pages) and metadata.get('pdf_text_contract')=='verbatim_v1',url=u,missing_pages=missing)
    x.dump('new-pdf-'+row['body_hash']+'.json',{'url':u,'sha256':row['body_hash'],'pages':len(reader.pages),'missing_pages':missing,'version_id':row['version_id']})
x.finish()
print('GAP_DIAGNOSTIC_ARTIFACT='+str(x.out))
