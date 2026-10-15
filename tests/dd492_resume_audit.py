"""Guarded ordinary-resume acceptance: deny and record any public DNS/socket use."""
import json
from collections import Counter
import sys
import psycopg
from dd492_pdf_acceptance import Acceptance, sha

x = Acceptance('20261014_160902')
guard = x.work / 'resume-network-guard'
guard.mkdir(exist_ok=True)
log = guard / 'network-attempts.jsonl'
log.write_text('')
(guard / 'sitecustomize.py').write_text('''import socket, os, json
_log = os.environ['SEAL_TEST_NETWORK_LOG']
with open(_log+'.active','w') as f: f.write('guard active')
_orig = socket.getaddrinfo
def guarded(host,*args,**kwargs):
    if host not in ('localhost','127.0.0.1','::1',None):
        with open(_log,'a') as f: f.write(json.dumps({'host':str(host)})+'\\n')
        raise RuntimeError('independent_acceptance_public_network_denied')
    return _orig(host,*args,**kwargs)
socket.getaddrinfo=guarded
_orig_connect=socket.socket.connect
def guarded_connect(self,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1'):
        with open(_log,'a') as f: f.write(json.dumps({'address':str(address)})+'\\n')
        raise RuntimeError('independent_acceptance_public_network_denied')
    return _orig_connect(self,address)
socket.socket.connect=guarded_connect
''')
x.env['PYTHONPATH'] = str(guard)
x.env['SEAL_TEST_NETWORK_LOG'] = str(log)
def counts():
    with psycopg.connect(x.env['SEAL_DATABASE_URL']) as c:
        return {t:c.execute('SELECT count(*) FROM seal.'+t).fetchone()[0] for t in ('run','observation','version')}
before=counts()
latest={r['url']:r for r in x.rows()}
manifest=json.loads((x.runner_out/'pdf-manifest.json').read_text())
selected=[latest[r['url']] for r in manifest]
parsed=sum(r['outcome'] in ('parsed','unchanged') for r in selected)
expected={'parsed':parsed,'failed':550-parsed}
expected={k:v for k,v in expected.items() if v}
files={str(p):sha(p.read_bytes()) for p in x.runner_out.glob('page-*.json')}
r=x.command('guarded-resume',['python','-m','seal.vietnam_documents',str(x.runner_out/'dd-492.yaml'),'--state',str(x.work/'harvest'),'--artifacts',str(x.runner_out),'--max-pages','10'])
summary=json.loads((x.runner_out/'summary.json').read_text())
x.check('guarded resume retains honest partial-result exit and terminal completion', r.returncode==int(parsed<550) and summary['selected_pages_complete'] and summary['counts']==expected, exit_code=r.returncode)
failure_counts=dict(Counter(r['error'] for r in selected if r['error']))
archived=sum(bool(r['archive_ref']) for r in selected)
x.check('summary exposes exact independent latest failure reasons and archived coverage',summary.get('failure_counts')==failure_counts and summary.get('latest_archived_pdfs')==archived and summary.get('all_latest_originals_archived')==(archived==550),expected_errors=failure_counts,archived=archived)
x.check('network guard is active in runner process', __import__('pathlib').Path(str(log)+'.active').exists())
x.check('ordinary resume makes zero public network attempts',not log.read_text(),attempts=log.read_text())
x.check('ordinary resume creates zero Runs Observations Versions',before==counts(),before=before,after=counts())
x.check('ordinary resume preserves frozen page evidence',files=={str(p):sha(p.read_bytes()) for p in x.runner_out.glob('page-*.json')})
failed=x.finish()
print('RESUME_ARTIFACT='+str(x.out))
sys.exit(failed)
