"""Fresh issue #33 CLI acceptance. Secrets remain in process environment/work only."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import boto3, psycopg, yaml
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path('/Users/yoryon/SEAL/.venv/bin/python')
WORK = ROOT / 'work/issue33-blind'
ART = ROOT / 'artifacts/issue33-blind'

def load_env():
    raw = subprocess.run(['zsh','-c','set -a; source /Users/yoryon/SEAL/work/dev/env.sh; env -0'], capture_output=True, check=True).stdout
    env = dict(os.environ)
    env.update(dict(s.decode().split('=',1) for s in raw.split(b'\0') if b'=' in s))
    env.update(PYTHONPATH=str(ROOT/'src'), SEAL_ALLOW_LOOPBACK='1', TESSDATA_PREFIX='/Users/yoryon/SEAL/work/ocr-tessdata')
    return env

def setup(phase):
    env=load_env(); state=WORK/f'{phase}-state.json'
    if state.exists():
        cfg=json.loads(state.read_text())
    else:
        suffix=str(int(time.time()))
        cfg={'database':f'seal_accept33_{phase}_{suffix}', 'bucket':f'seal-accept33-{phase}-{suffix}'}
        p=urlsplit(env['SEAL_DATABASE_URL'])
        admin=urlunsplit((p.scheme,p.netloc,'/postgres',p.query,p.fragment))
        with psycopg.connect(admin,autocommit=True) as c:
            c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cfg['database'])))
        s3=boto3.client('s3',endpoint_url=env['SEAL_S3_ENDPOINT'],aws_access_key_id=env['SEAL_S3_ACCESS_KEY'],aws_secret_access_key=env['SEAL_S3_SECRET_KEY'],region_name=env.get('SEAL_S3_REGION','us-east-1'))
        s3.create_bucket(Bucket=cfg['bucket']); state.write_text(json.dumps(cfg))
    p=urlsplit(env['SEAL_DATABASE_URL'])
    env.update(SEAL_DATABASE_URL=urlunsplit((p.scheme,p.netloc,'/'+cfg['database'],p.query,p.fragment)), SEAL_S3_BUCKET=cfg['bucket'], SEAL_WORK_DIR=str(WORK/phase/'runtime'))
    (ART/phase).mkdir(parents=True,exist_ok=True); (WORK/phase).mkdir(parents=True,exist_ok=True)
    return env

def cli(env, args, label, phase, timeout=1800):
    cmd=[str(PYTHON),'-m','seal',*args]
    start=time.monotonic()
    r=subprocess.run(cmd,cwd=ROOT,env=env,capture_output=True,text=True,timeout=timeout)
    out=ART/phase/f'{label}.json'
    try: data=json.loads(r.stdout)
    except Exception: data={'stdout':r.stdout}
    record={'argv':['python','-m','seal',*args],'returncode':r.returncode,'elapsed_seconds':round(time.monotonic()-start,3),'result':data,'stderr_tail':r.stderr[-3000:]}
    out.write_text(json.dumps(record,ensure_ascii=False,indent=2))
    print(json.dumps({'label':label,'returncode':r.returncode,'status':data.get('status'),'error':data.get('error'),'run_id':data.get('run_id')},ensure_ascii=False),flush=True)
    return data,r.returncode

def freeze_configs():
    for sid in ('dd-492','dd-499'):
        cfg=yaml.safe_load((ROOT/f'sources/{sid}.yaml').read_text())
        if sid=='dd-492':
            cfg['adapter_options']['max_pages']=2
            cfg['budget'].update(documents=3,requests=40,seconds=900)
            cfg['concurrency']=2
        else:
            cfg['adapter_options'].update(per_page=2,max_pages=1)
            cfg['budget'].update(documents=2,requests=20,seconds=600)
        dest=WORK/'real'/f'{sid}.yaml'; dest.write_text(yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False))
        shutil.copy2(dest,ART/'real'/f'{sid}.yaml')

def audit_sql(env,phase,label):
    queries={
      'sources':'SELECT id FROM seal.source ORDER BY id',
      'runs':'SELECT id,source_id,status,errors FROM seal.run ORDER BY created_at',
      'resources':'SELECT id,frontier_id,identity_namespace,external_key,public_url,current_version_id FROM seal.resource ORDER BY external_key',
      'versions':'SELECT id,resource_id,result_ref,inputs_ref,input_fingerprint,parser_fingerprint,content_hash FROM seal.version ORDER BY id',
      'acquisitions':'SELECT id,run_id,http_method,request_url,actual_url,acquisition_kind,state,outcome,http_status,body_hash,archive_ref FROM seal.acquisition ORDER BY started_at',
      'frontiers':'SELECT id,source_id,checkpoint FROM seal.frontier ORDER BY id',
    }
    result={}
    with psycopg.connect(env['SEAL_DATABASE_URL'],row_factory=dict_row) as c:
        for name,q in queries.items():
            try:
                with c.transaction(): result[name]=c.execute(q).fetchall()
            except Exception as e: result[name]={'error':str(e).splitlines()[0]}
    (ART/phase/f'{label}-sql.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['real','audit-real','controlled','deliver-real','deliver-controlled']);p.add_argument('--fresh',action='store_true');args=p.parse_args()
    if args.fresh:
        phase='controlled' if args.mode in ['controlled','deliver-controlled'] else 'real'
        (WORK/f'{phase}-state.json').unlink(missing_ok=True)
    if args.mode=='controlled':
        return controlled()
    if args.mode=='deliver-controlled':
        return delivery(setup('controlled'),'controlled')
    env=setup('real')
    if args.mode=='deliver-real':
        return delivery(env,'real')
    if args.mode=='real':
        cli(env,['init'],'init','real');freeze_configs()
        for sid in ('dd-492','dd-499'):
            cli(env,['collect',str(WORK/'real'/f'{sid}.yaml'),'--no-progress'],f'{sid}-collect','real')
        audit_sql(env,'real','after-collect')
        return delivery(env,'real')
    else: audit_sql(env,'real','after-collect')


def query(env,statement,params=()):
    with psycopg.connect(env['SEAL_DATABASE_URL'],row_factory=dict_row) as c:
        return c.execute(statement,params).fetchall()

def s3_client(env):
    return boto3.client('s3',endpoint_url=env['SEAL_S3_ENDPOINT'],aws_access_key_id=env['SEAL_S3_ACCESS_KEY'],aws_secret_access_key=env['SEAL_S3_SECRET_KEY'],region_name=env.get('SEAL_S3_REGION','us-east-1'))

def object_bytes(env,key):
    body=s3_client(env).get_object(Bucket=env['SEAL_S3_BUCKET'],Key=key)['Body'].read()
    assert hashlib.sha256(body).hexdigest()==key, ('CAS_hash',key)
    return body

def resource_state(env):
    return {r['external_key']:r for r in query(env,'SELECT r.id,r.external_key,r.public_url,r.current_version_id,count(v.id) AS versions FROM seal.resource r LEFT JOIN seal.version v ON v.resource_id=r.id GROUP BY r.id ORDER BY r.external_key')}

def controlled():
    from issue33_blind_fixture import Fixture
    phase='controlled'; env=setup(phase);cli(env,['init'],'init',phase)
    fixture=Fixture();checks=[]
    try:
        configs={}
        for sid,adapter,entry in [('dd-492','vietnam_law',fixture.base+'/vn/'),('dd-499','federal_register',fixture.base+'/api')]:
            cfg={'id':sid,'adapter':adapter,'entry_urls':[entry],'allowed_hosts':['127.0.0.1'],'allowed_path_prefixes':['/'],'archive_approved':True,'scope':'Isolated complete legal-text acceptance fixture; not public historical coverage.','robots':False,'recheck_seconds':1,'concurrency':2,'delay':0.0,'budget':{'requests':50,'seconds':120,'response_bytes':10485760,'documents':10},'fetch':{'mode':'http','max_depth':2,'page_timeout_ms':30000,'retries':0},'metadata':{'source_name':'Independent issue33 acceptance fixture','document_type':'法律法规','language':'vi' if sid=='dd-492' else 'en','jurisdiction':'VN' if sid=='dd-492' else 'US'}}
            cfg['adapter_options']={'max_pages':2} if sid=='dd-492' else {'per_page':2,'max_pages':1,'listing_body_field':'body'}
            if sid=='dd-492':cfg['documents']={'detail_body':'never','attachment_title':'link_text'}
            path=WORK/phase/f'{sid}.yaml';path.write_text(yaml.safe_dump(cfg,allow_unicode=True,sort_keys=False));shutil.copy2(path,ART/phase/f'{sid}.yaml');configs[sid]=path
        for sid in configs:
            data,rc=cli(env,['collect',str(configs[sid]),'--no-progress'],f'{sid}-baseline',phase)
            checks.append({'check':sid+' baseline successful','passed':rc==0,'status':data.get('status')})
        before=resource_state(env)
        for r in query(env,"SELECT r.external_key,v.result_ref FROM seal.resource r JOIN seal.version v ON v.id=r.current_version_id WHERE r.external_key IN ('9001','9002')"):
            doc=json.loads(object_bytes(env,r['result_ref']))
            if r['external_key']=='9001':
                ds=doc['deliverables'];ok=sorted(d['volume_order'] for d in ds if d['role']=='volume')==[1,2] and sum(d['role']=='independent_attachment' for d in ds)==1
                checks.append({'check':'ordered 1,2 volumes plus independent attachment','passed':ok})
            else:
                md=doc['content']['markdown'];ok='CHÍNH PHỦ' in md and 'Điều 1.' in md and 'Ban hành quy định' in md and any(d.get('legacy_encoding')=='tcvn3' for d in doc['extraction']['deliverables'])
                checks.append({'check':'mixed TCVN3 recovery preserves Unicode header','passed':ok,'markdown':md})
        (ART/phase/'baseline-resources.json').write_text(json.dumps(before,indent=2,default=str))
        for sid in configs:cli(env,['collect',str(configs[sid]),'--no-progress'],f'{sid}-repeat',phase)
        repeat=resource_state(env)
        checks.append({'check':'repeat preserves resource IDs and versions','passed':before==repeat})
        fixture.api_variant=1
        cli(env,['collect',str(configs['dd-499']),'--no-progress'],'dd-499-reordered-unrelated-change',phase)
        after=resource_state(env)
        for key in ['2026-90001','2026-90002']:
            ok=(after[key]['id']==before[key]['id'] and after[key]['versions']==before[key]['versions']+(key=='2026-90002'))
            checks.append({'check':'shared JSON identity/version '+key,'passed':ok,'before':before[key],'after':after[key]})
        checks.append({'check':'no public_url supported','passed':all(before[k]['public_url'] is None for k in ['2026-90001','2026-90002'])})
        for mode in ['missing','quality','ambiguous','structure','out-of-scope','challenge']:
            fixture.mode=mode
            prior=resource_state(env)
            data,rc=cli(env,['collect',str(configs['dd-492']),'--no-progress'],'dd-492-failure-'+mode,phase)
            current=resource_state(env)
            versions_same=all(current[k]['versions']==v['versions'] for k,v in prior.items())
            checks.append({'check':'failure explicit and no partial Version: '+mode,'passed':rc!=0 and versions_same,'status':data.get('status'),'errors':data.get('errors'),'versions_same':versions_same})
        fixture.mode='api-pagination'
        data,rc=cli(env,['collect',str(configs['dd-499']),'--no-progress'],'dd-499-page-limit',phase)
        coverage=data.get('report',{}).get('coverage',{})
        checks.append({'check':'next_page beyond max_pages reports incomplete enumeration','passed':coverage.get('selected_scope_enumeration')=='incomplete','coverage':coverage})
        for mode in ['api-quality','api-missing-body','api-structure']:
            fixture.mode=mode
            prior=resource_state(env)
            data,rc=cli(env,['collect',str(configs['dd-499']),'--no-progress'],'dd-499-failure-'+mode,phase)
            current=resource_state(env)
            versions_same=all(current[k]['versions']==v['versions'] for k,v in prior.items())
            checks.append({'check':'API failure explicit and no partial Version: '+mode,'passed':rc!=0 and versions_same,'status':data.get('status'),'errors':data.get('errors'),'versions_same':versions_same})
        fixture.mode='normal'
        queued,rc=cli(env,['collect',str(configs['dd-499']),'--enqueue'],'dd-499-worker-enqueue',phase)
        cli(env,['worker','--once'],'dd-499-worker-once',phase)
        worker_run=query(env,'SELECT status FROM seal.run WHERE id=%s',(queued['run_id'],))[0]
        checks.append({'check':'enqueued official CLI worker completes persisted run','passed':worker_run['status']=='complete','status':worker_run['status']})
        (ART/phase/'fixture-http-events.json').write_text(json.dumps(fixture.events,ensure_ascii=False,indent=2))
        audit_sql(env,phase,'after-controlled')
        (ART/phase/'assertions.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2,default=str))
        print(json.dumps(checks,ensure_ascii=False,default=str),flush=True)
        delivery(env,phase)
        assert all(c['passed'] for c in checks), 'business_assertion_failed; see assertions.json'
    finally:fixture.close()

def delivery(env,phase):
    import urllib.request
    from cryptography.fernet import Fernet
    from openpyxl import load_workbook
    snapshot,rc=cli(env,['snapshot','create'],'snapshot-create',phase);assert rc==0,snapshot
    sid=snapshot['snapshot_id'];_,rc=cli(env,['snapshot','publish',sid],'snapshot-publish',phase);assert rc==0
    _,rc=cli(env,['snapshot','inspect',sid,'--limit','100'],'snapshot-inspect',phase);assert rc==0
    port=19333 if phase=='real' else 19334
    token=os.urandom(24).hex()
    config={'public_base_url':f'http://127.0.0.1:{port}','token_key':Fernet.generate_key().decode(),'callers':[{'id':'accept33','key_sha256':hashlib.sha256(token.encode()).hexdigest(),'sources':['dd-492','dd-499']}], 'workers':2}
    apipath=WORK/phase/'api-private.json';apipath.write_text(json.dumps(config));apipath.chmod(0o600)
    process=subprocess.Popen([str(PYTHON),'-m','seal','serve','--config',str(apipath),'--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    checked=[];results={};api_files={}
    def request(path):
        req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(req,timeout=30) as res:return res.read(),dict(res.headers)
    try:
        for _ in range(60):
            try:request('/healthz');break
            except Exception:time.sleep(0.1)
        for source in ['dd-492','dd-499']:
            raw,_=request('/results?source_id='+source+'&limit=100');items=json.loads(raw);results[source]=items
            assert items['snapshot_id']==sid
            for row in items['items']:
                for key in ['文件哈希','markdown_hash']:
                    h=row[key]
                    if h not in api_files:
                        body,headers=request('/files/'+h)
                        assert hashlib.sha256(body).hexdigest()==h
                        assert body==object_bytes(env,h)
                        api_files[h]={'bytes':len(body),'content_type':headers.get('Content-Type'),'sha256':h}
                checked.append({'source_id':source,'resource_id':row['resource_id'],'version_id':row['version_id'],'row_number':row['row_number'],'file_hash':row['文件哈希'],'markdown_hash':row['markdown_hash'],'source_record_hash':row['来源记录号']})
    finally:
        process.terminate();process.wait(timeout=20)
    (ART/phase/'api-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2));(ART/phase/'api-files.json').write_text(json.dumps(api_files,ensure_ascii=False,indent=2))
    out=ART/phase/'delivery'
    if out.exists():shutil.rmtree(out)
    exported,rc=cli(env,['export','--snapshot',sid,'--output',str(out)],'export',phase);assert rc==0,exported
    rows=list(load_workbook(out/'metadata.xlsx',read_only=True,data_only=True).active.values)
    assert len(rows[0])==16
    manifest=json.loads((out/'manifest.json').read_text())
    data_rows=rows[manifest['data_start_row']-1:]
    assert len(data_rows)==len(checked),('row_count',len(data_rows),len(checked))
    flat=[r for data in results.values() for r in data['items']]
    for mr in manifest['rows']:
        match=next(r for r in flat if r['resource_id']==mr['resource_id'] and r['version_id']==mr['version_id'] and r['markdown_hash']==mr['markdown_hash'])
        values=rows[mr['row']-1]
        for col,value in zip(rows[0],values):
            assert ('' if value is None else value)==match[col],('API_workbook_cell',mr['row'],col,value,match[col])
    for file in manifest['files']:
        assert hashlib.sha256((out/file['path']).read_bytes()).hexdigest()==file['sha256']
        if file['path'].startswith(('markdown/','originals/')):
            assert (out/file['path']).read_bytes()==object_bytes(env,file['sha256'])
    sqlrows=query(env,'SELECT d.*,r.external_key FROM seal.delivery_row d JOIN seal.version v ON v.id=d.version_id JOIN seal.resource r ON r.id=v.resource_id JOIN seal.snapshot_item si ON si.version_id=v.id WHERE si.snapshot_id=%s ORDER BY d.version_id,d.row_number',(sid,))
    for r in sqlrows:
        assert any(x['version_id']==r['version_id'] and x['row_number']==r['row_number'] and x['file_hash']==r['file_hash'] and x['markdown_hash']==r['markdown_hash'] for x in checked),('SQL_API_row',r)
    refs=[]
    lineage_checks=[]
    for v in query(env,'SELECT id,result_ref,inputs_ref FROM seal.version ORDER BY id'):
        for kind in ['result_ref','inputs_ref']:
            body=object_bytes(env,v[kind]);obj=json.loads(body)
            dest=ART/phase/'cas'/f'{v[kind]}.json';dest.parent.mkdir(exist_ok=True);dest.write_bytes(body)
            refs.append({'version_id':v['id'],'kind':kind,'sha256':v[kind],'schema':obj.get('schema')})
            if kind=='inputs_ref':
                for inp in obj['inputs']:
                    original=object_bytes(env,inp['archive_ref'])
                    loc=inp['locator']
                    if 'json_pointer' in loc:
                        leaf=json.loads(original)
                        for token in loc['json_pointer'].strip('/').split('/') if loc['json_pointer'] else []:
                            token=token.replace('~1','/').replace('~0','~')
                            leaf=leaf[int(token)] if isinstance(leaf,list) else leaf[token]
                        assert leaf['document_number']==obj['document']['external_key']
                    if 'xpath' in loc:
                        from lxml import etree
                        assert etree.fromstring(original).xpath(loc['xpath'])
                    lineage_checks.append({'version_id':v['id'],'archive_ref':inp['archive_ref'],'locator':loc,'resolved':True})
    moved=ART/phase/'moved-offline-delivery'
    if moved.exists():shutil.rmtree(moved)
    shutil.move(out,moved)
    block=WORK/'offline-block';block.mkdir(exist_ok=True)
    (block/'sitecustomize.py').write_text('import socket\ndef denied(*args, **kwargs):\n    raise RuntimeError("network_disabled_for_offline_verify")\nsocket.socket.connect = denied\nsocket.socket.connect_ex = denied\n')
    offline={k:v for k,v in env.items() if not k.startswith(('SEAL_DATABASE','SEAL_S3'))}
    offline['PYTHONPATH']=str(block)+os.pathsep+str(ROOT/'src')
    verified,rc=cli(offline,['export','verify',str(moved)],'moved-offline-verify',phase);assert rc==0,verified
    source_hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'src/seal').rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
    (ART/phase/'source-hashes.json').write_text(json.dumps(source_hashes,indent=2))
    reports={}
    for source in ['dd-492','dd-499']:
        current=query(env,'SELECT r.external_key,v.result_ref FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id JOIN seal.version v ON v.id=r.current_version_id WHERE f.source_id=%s',(source,))
        docs=[json.loads(object_bytes(env,r['result_ref'])) for r in current]
        reports[source]={'current_deliverable_documents':len(docs),'delivery_rows':sum(len(d['deliverables']) for d in docs),'original_acquired_documents':sum(any(x['original_file_ref'] for x in d['deliverables']) for d in docs),'quality_passed_documents':sum(all(x['quality_report'].get('recommended_route') in ['accept','accept_ocr','accept_with_warning'] for x in d['deliverables']) for d in docs),'full_history_denominator':'UNKNOWN','extraction_methods':sorted({str(x.get('method') or x.get('extractor') or x.get('kind')) for d in docs for x in d['extraction']['deliverables']})}
    report={'snapshot_id':sid,'API_CAS_SHA256_equal':True,'SQL_API_rows_equal':True,'workbook_headers':rows[0],'workbook_rows':len(data_rows),'api_rows':len(checked),'manifest_schema':manifest.get('schema'),'snapshot_coverage':manifest.get('coverage'),'references':refs,'lineage_checks':lineage_checks,'moved_offline_verify':verified,'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'test_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'full_history_coverage':'UNKNOWN','source_summary':reports,'source_tree_sha256':hashlib.sha256(json.dumps(source_hashes,sort_keys=True).encode()).hexdigest(),'fixture_test_sha256':hashlib.sha256((ROOT/'tests/issue33_blind_fixture.py').read_bytes()).hexdigest()}
    (ART/phase/'delivery-reconciliation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    print(json.dumps({'phase':phase,'rows':len(checked),'offline_verify':rc}),flush=True)

if __name__=='__main__':main()
