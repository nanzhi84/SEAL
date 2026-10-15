"""Read-only independent audit of the single eleven-URL changed-budget run."""
import io
import re
from lxml import html
import json
import subprocess
import sys
from pypdf import PdfReader
from dd492_pdf_acceptance import Acceptance, sha

x=Acceptance('20261014_160902')
prior=__import__('pathlib').Path(sys.argv[1]) if len(sys.argv)>1 else x.runner_out.parent/'retry-20261015_103029'
filename=sys.argv[2] if len(sys.argv)>2 else 'eleven-gap-after.json'
rows=json.loads((prior/filename).read_text())
reports=[]
for row in rows:
    if not row['archive_ref']:
        x.check('unarchived PDF remains explicit without Version',row['outcome'] in ('parse_failed','network_failed') and row['error'] in ('response_byte_budget_exceeded','download_failed') and not row['version_id'],url=row['url'])
        reports.append({'url':row['url'],'status':row['error'],'archive_ref':None})
        continue
    body=x.object(row['archive_ref'])
    x.check('new original archive digest and attachment reference agree',sha(body)==row['body_hash']==row['archive_ref'] and row['body_hash'] in row['attachment_refs'],url=row['url'])
    reader=PdfReader(io.BytesIO(body))
    native=[p.extract_text() or '' for p in reader.pages]
    p=x.work/'pdf-audit'/(row['body_hash']+'.pdf');p.write_bytes(body)
    result=subprocess.run(['pdftotext','-layout',str(p),'-'],capture_output=True)
    poppler=result.stdout.decode('utf-8',errors='replace')
    if row['version_id']:
        result=x.envelope(row).get('result',{})
        nodes=html.fromstring(result.get('cleaned_html') or '<div/>').xpath('//*[@data-page]')
        x.check('new successful large original retains all ordered pages', [int(n.attrib['data-page']) for n in nodes]==list(range(1,len(native)+1)),url=row['url'])
        missing=[i+1 for i,t in enumerate(native) if i>=len(nodes) or re.sub(r'\s+','',t)!=re.sub(r'\s+','',nodes[i].text_content())]
        x.check('new successful large original retains complete original text',not missing and result.get('metadata',{}).get('pdf_text_contract')=='verbatim_v1',url=row['url'],missing_pages=missing)
        reports.append({'url':row['url'],'status':'PARSED','sha256':row['body_hash'],'pages':len(native),'missing_pages':missing})
        continue
    x.check('new no-text source explicitly fails without successful Version',row['outcome']=='parse_failed' and row['error']=='pdf_text_layer_required' and not row['version_id'],url=row['url'])
    reports.append({'url':row['url'],'status':'NO_BODY_TEXT','sha256':row['body_hash'],'pages':len(native),'native_text_characters':sum(map(len,native)),'native_text':'\n'.join(native),'poppler_exit':result.returncode,'poppler_text':poppler,'poppler_text_sha256':sha(result.stdout)})
x.dump('new-originals-poppler-crosscheck.json',reports)
x.check('all diagnosed originals acquired and archived',all(r['archive_ref'] for r in rows),missing=[r['url'] for r in rows if not r['archive_ref']])
failed=x.finish();print('GAP_AUDIT_ARTIFACT='+str(x.out));sys.exit(failed)
