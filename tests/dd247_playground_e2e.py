"""Live E2E. Failure boundaries: API declaration/empty filters, missing or duplicate
pages, changing native total, JSON loss, corrupt archive/hash/lineage, missing
network recheck observation, incomplete CSV. All data under playgroud/dd-247.
Requires PostgreSQL createdb, frozen uv dependencies and public AMAC access.
"""
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
import psycopg
from psycopg.rows import dict_row
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'playgroud/dd-247'
ENTRY = 'https://www.amac.org.cn/fwdt/wyc/jgcprycx/jgcx/gmjjglrml/'
API = '/portal/front/mutualFund/findMutualFundHousePage'
run = sys.argv[1] if len(sys.argv)>1 else datetime.now().strftime('dd247-e2e-%Y%m%d-%H%M%S')
assert all(c.isalnum() or c in '-_' for c in run)
db = 'seal_' + run.replace('-', '_')
art, work = BASE/'artifacts'/run, BASE/'work'/run
art.mkdir(parents=True, exist_ok=False)
work.mkdir(parents=True, exist_ok=False)
env = dict(os.environ, SEAL_DATABASE_URL='postgresql:///'+db, SEAL_ARCHIVE=str(work/'archive'))
config = BASE/'dd-247.yaml'
shutil.copyfile(config, art/'dd-247.yaml')
checks = []
def check(name, ok, detail=None):
    checks.append(dict(assertion=name, passed=bool(ok), detail=detail))
    if not ok: raise AssertionError(name)
def command(name, args):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)
    (art/(name+'.stdout')).write_text(result.stdout)
    (art/(name+'.stderr')).write_text(result.stderr)
    check(name+' exit', result.returncode==0)
    return result.stdout

def obj(key):
    data=(work/'archive/objects'/key[:2]/key[2:]).read_bytes()
    check('archive hash '+key, hashlib.sha256(data).hexdigest()==key)
    return data

(art/'environment.json').write_text(json.dumps(dict(command='uv run --frozen python tests/dd247_playground_e2e.py '+run,cwd=str(ROOT),database=db,archive=env['SEAL_ARCHIVE'],python=sys.version,git_head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),prerequisites='Local PostgreSQL createdb; uv frozen dependencies; public AMAC network'),ensure_ascii=False,indent=2))
status='FAIL'
try:
    cfg=yaml.safe_load(config.read_text())
    check('source and entry',cfg['id']=='dd-247' and cfg['entry_urls']==[ENTRY])
    command('createdb',['createdb',db])
    command('init',['uv','run','--frozen','seal','init'])
    receipt=json.loads(command('dd-247-collect',['uv','run','--frozen','seal','collect',str(config)]))
    check('collect complete',receipt['status']=='complete',receipt.get('errors'))
    with psycopg.connect(env['SEAL_DATABASE_URL'],row_factory=dict_row) as con:
        def query(sql,args=()): return con.execute(sql,args).fetchall()
        observations=query('SELECT * FROM seal.fetch_observation WHERE run_id=%s',(receipt['run_id'],))
        entry=next(o for o in observations if o['url']==ENTRY and o['status']==200)
        source=obj(json.loads(obj(entry['snapshot_id']))['body_hash']).decode()
        check('archived entry declares API and pagination',API in source and 'pageNo:' in source and 'pageSize:' in source)
        docs=query('SELECT d.url,v.* FROM seal.document d JOIN seal.document_version v ON v.id=d.latest_document_version WHERE d.source_id=%s',('dd-247',))
        api_docs=[d for d in docs if urlsplit(d['url']).path==API]
        check('API documents exist',bool(api_docs))
        totals, pages, rows, evidence=set(),set(),[],[]
        before={}
        for d in api_docs:
            qs=parse_qs(urlsplit(d['url']).query,keep_blank_values=True)
            check('empty native filters '+d['url'],all(qs.get(k)==[''] for k in ('houseName','registerAddr','officeAddr')) and qs['pageSize']==['10'])
            page=int(qs['pageNo'][0]); check('unique page '+str(page),page not in pages); pages.add(page)
            body=obj(d['body_hash']); raw=json.loads(body)
            check('native success envelope',raw['code']==200 and raw['data']['errcode']==0)
            data=raw['data']['data']; totals.add(data['total']); rows.extend(data['dataList'])
            check('native page size '+str(page),len(data['dataList'])==min(10,max(0,data['total']-10*(page-1))))
            check('full JSON fidelity '+str(page),d['source_format']=='json' and d['content']['children']==[dict(type='json',value=raw)])
            canonical=json.dumps(d['content'],sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
            check('content hash '+str(page),hashlib.sha256(canonical).hexdigest()==d['content_hash'])
            check('version revision observation linkage '+str(page),bool(query('SELECT v.id FROM seal.document_version v JOIN seal.revision r ON r.id=v.revision_id JOIN seal.fetch_observation o ON o.id=v.observation_id WHERE v.id=%s AND v.body_hash=r.body_hash AND v.body_hash=o.body_hash AND v.snapshot_id=o.snapshot_id AND o.run_id=%s',(d['id'],receipt['run_id']))))
            discovery=query('SELECT * FROM seal.discovery WHERE run_id=%s AND url=%s AND parent_snapshot_id IS NOT NULL AND parent_observation_id IS NOT NULL',(receipt['run_id'],d['url']))
            check('discovery lineage '+str(page),bool(discovery))
            if page==1: check('API discovered from archived entry',any(x['parent_url']==ENTRY and x['parent_snapshot_id']==entry['snapshot_id'] and x['parent_observation_id']==entry['id'] for x in discovery))
            evidence.append(dict(page=page,url=d['url'],rows=len(data['dataList']),total=data['total'],body_hash=d['body_hash'],version_id=d['id'],observation_id=d['observation_id'],discovery=discovery))
            before[d['url']]=d
        check('stable native total',len(totals)==1)
        total=next(iter(totals))
        check('complete native page sequence',pages==set(range(1,max(1,math.ceil(total/10))+1)))
        check('row count equals native total',len(rows)==total,dict(total=total,actual=len(rows)))
        check('no duplicate native rows',len({json.dumps(r,sort_keys=True,ensure_ascii=False) for r in rows})==len(rows))
        (art/'dd-247-page-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str))
        (art/'dd-247-native-rows.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    queued=json.loads(command('dd-247-recheck',['uv','run','--frozen','seal','recheck',str(config),'--enqueue']))
    command('worker',['uv','run','--frozen','seal','worker','--once'])
    with psycopg.connect(env['SEAL_DATABASE_URL'],row_factory=dict_row) as con:
        state=con.execute('SELECT * FROM seal.run WHERE id=%s',(queued['run_id'],)).fetchone()
        check('recheck complete',state['status']=='complete',state['errors'])
        emissions=con.execute('SELECT e.*,d.url,v.body_hash FROM seal.document_emission e JOIN seal.document d ON d.id=e.document_id JOIN seal.document_version v ON v.id=e.document_version_id WHERE e.run_id=%s',(queued['run_id'],)).fetchall()
        check('recheck API coverage',set(before)<={e['url'] for e in emissions})
        for e in emissions:
            obs=con.execute('SELECT * FROM seal.fetch_observation WHERE id=%s',(e['observation_id'],)).fetchone()
            check('fresh network recheck observation',obs['run_id']==queued['run_id'] and obs['origin']=='network')
            obj(obs['snapshot_id']); obj(obs['body_hash'])
            if e['url'] in before and e['body_hash']==before[e['url']]['body_hash']:
                check('stable body reuses version',e['document_version_id']==before[e['url']]['id'] and e['change_kind']=='unchanged')
        (art/'dd-247-recheck-evidence.json').write_text(json.dumps(emissions,ensure_ascii=False,indent=2,default=str))
        export=art/'dd-247-documents.csv'
        with export.open('wb') as file,con.cursor().copy("COPY (SELECT d.id,d.url,v.id AS version_id,v.content FROM seal.document d JOIN seal.document_version v ON v.id=d.latest_document_version WHERE d.source_id='dd-247' ORDER BY d.url) TO STDOUT CSV HEADER") as copy:
            for data in copy:file.write(data)
        exported=list(csv.DictReader(export.open()))
        check('CSV exact Document coverage',{r['url'] for r in exported}=={d['url'] for d in docs})
        check('CSV JSON fidelity',all(json.loads(r['content'])['children'] for r in exported))
    status='PASS'
except Exception as error:
    checks.append(dict(assertion='execution',passed=False,detail=repr(error)))
finally:
    (art/'acceptance.json').write_text(json.dumps(dict(status=status,run=run,database=db,archive=str(work/'archive'),scope='Archived entry and empty-filter native API pagination only; no whole-site completeness or factual accuracy claim',assertions=checks),ensure_ascii=False,indent=2))
    print(json.dumps(dict(status=status,run=run,artifacts=str(art),assertions=len(checks)),ensure_ascii=False))
    sys.exit(0 if status=='PASS' else 1)
