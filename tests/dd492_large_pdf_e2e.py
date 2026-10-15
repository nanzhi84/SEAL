"""Fresh64MiB PDF collect→Snapshot→authorized API E2E. No product imports.
uv run --no-sync python tests/dd492_large_pdf_e2e.py
"""
import io
import json
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,urlopen
import yaml
from cryptography.fernet import Fernet
from pypdf import PdfReader,PdfWriter
from pypdf.generic import DecodedStreamObject
from dd492_pdf_acceptance import Acceptance,Handler,pdf,sha

CAP=134217728
x=Acceptance()
server=service=None
keys=[secrets.token_urlsafe(40) for _ in range(2)]
token_key=Fernet.generate_key().decode()

def request(path,key=None):
    headers={} if key is None else {'Authorization':'Bearer '+keys[key]}
    try:r=urlopen(Request(path,headers=headers),timeout=60)
    except HTTPError as e:r=e
    with r:return r.status,r.read()

def stop():
    global service
    if service:
        service.terminate();service.wait(timeout=15);service=None

def start(cap=None,invalid=False,allowed=True):
    global service
    stop()
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    config={'public_base_url':base,'token_key':token_key,'callers':[
        {'id':'allowed','key_sha256':sha(keys[0].encode()),'sources':['dd-492'] if allowed else ['dd-001']},
        {'id':'denied','key_sha256':sha(keys[1].encode()),'sources':['dd-001']}]}
    if cap is not None:config['attachment_max_bytes']=cap
    path=x.work/f'api-{port}.private.json';path.write_text(json.dumps(config));path.chmod(0o600)
    argv=['uv','run','--no-sync','seal','serve','--config',str(path),'--host','127.0.0.1','--port',str(port)]
    logfile=(x.work/f'api-{port}.private.log').open('w')
    service=subprocess.Popen(argv,env=x.env,stdout=logfile,stderr=subprocess.STDOUT)
    if invalid:
        try:service.wait(timeout=8)
        except subprocess.TimeoutExpired:pass
        x.check('API rejects configuration over128MiB',service.poll() is not None and service.returncode!=0)
        stop();return base
    for _ in range(200):
        if service.poll() is not None:raise RuntimeError('API startup failed; private log retained')
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.1):return base
        except OSError:time.sleep(.05)
    raise RuntimeError('API startup timeout')

try:
    w=PdfWriter();w.append(PdfReader(io.BytesIO(pdf(1))))
    stream=DecodedStreamObject();stream.set_data(b'X'*(64*1024*1024));w._add_object(stream)
    buf=io.BytesIO();w.write(buf);body=buf.getvalue();digest=sha(body)
    x.check('independent legal fixture exceeds50MiB and fits128MiB',52428800<len(body)<CAP and len(PdfReader(io.BytesIO(body)).pages)==1,bytes=len(body),sha256=digest)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.requests=[];server.bodies={'/large.pdf':body}
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}/large.pdf'
    cfg={'id':'dd-492','entry_urls':[url],'allowed_hosts':['127.0.0.1'],'host_path_scopes':{'127.0.0.1':['/']},'archive_approved':True,'scope':'isolated large original PDF acceptance','robots':False,'budget':{'requests':5,'seconds':180,'response_bytes':CAP},'crawl4ai':{'mode':'http','max_depth':0,'page_timeout_ms':60000,'word_count_threshold':1}}
    path=x.work/'large-source.yaml';path.write_text(yaml.safe_dump(cfg));x.dump('source-config.json',cfg)
    r=x.command('collect128',['seal','collect',str(path),'--no-progress'])
    row=[r for r in x.rows() if r['url']==url][-1]
    x.check('explicit128MiB Source collects large text PDF with successful Version',r.returncode==0 and row['sequence']==1 and row['outcome']=='parsed',outcome=row['outcome'],error=row['error'])
    x.check('large original preserves exact SHA and bytes',row['archive_ref']==digest and x.object(digest)==body,bytes=len(body))
    env=x.envelope(row);x.check('large original text and page count retained','INDEPENDENT_PAGE_001_COMPLETE_TEXT' in x.text(env) and env['result']['metadata']['pages']==1)
    create=x.command('snapshot-create',['seal','snapshot','create']);payload=json.loads(create.stdout);ident=payload.get('snapshot_id',payload.get('id'))
    x.check('Snapshot accepts large successful original',create.returncode==0 and bool(ident))
    publish=x.command('snapshot-publish',['seal','snapshot','publish',ident]);x.check('Snapshot publishes large original',publish.returncode==0)
    base=start(CAP);status,raw=request(base+'/results?'+urlencode({'source_url':url}),0);items=json.loads(raw)
    x.check('authorized published result exposes large attachment',status==200 and len(items)==1 and len(items[0]['attachments'])==1)
    attachment=items[0]['attachments'][0]['url']
    status,raw=request(attachment,0);x.check('explicit128MiB API downloads exact complete large original',status==200 and len(raw)==len(body) and sha(raw)==digest,bytes=len(raw),sha256=sha(raw))
    status,_=request(attachment);x.check('large attachment requires authentication',status==401,actual_status=status)
    status,_=request(attachment,1);x.check('large attachment retains source authorization',status==404,actual_status=status)
    base=start(CAP,allowed=False);status,raw=request(base+'/attachments/'+attachment.rsplit('/',1)[1],0);x.check('same caller loses access after source authorization revoked',status==403,actual_status=status)
    base=start();status,raw=request(base+'/results?'+urlencode({'source_url':url}),0)
    x.check('default API cap remains50MiB',status==500 and json.loads(raw)=={'error':'archive_too_large'},actual_status=status)
    start(CAP+1,invalid=True)
    bad={**cfg,'budget':{**cfg['budget'],'response_bytes':CAP+1}};path=x.work/'source-over-cap.yaml';path.write_text(yaml.safe_dump(bad))
    before=len(x.rows());r=x.command('source-over-cap',['seal','collect',str(path),'--no-progress']);x.check('Source rejects response budget over128MiB before collection',r.returncode!=0 and len(x.rows())==before)
    # Public defaults are exercised via actual CLI behavior, never model imports.
    default=dict(cfg);default.pop('budget');path=x.work/'source-default-cap.yaml';path.write_text(yaml.safe_dump(default))
    r=x.command('source-default-cap',['seal','collect',str(path),'--no-progress']);row=[r for r in x.rows() if r['url']==url][-1]
    x.check('default Source budget still rejects64MiB original',row['error']=='response_byte_budget_exceeded' and not row['archive_ref'],outcome=row['outcome'],error=row['error'])
except Exception as e:
    x.check('large attachment E2E completes',False,error=type(e).__name__+': '+str(e))
finally:
    stop()
    if server:server.shutdown();server.server_close()
failed=x.finish();print('LARGE_E2E_ARTIFACT='+str(x.out));sys.exit(failed)
