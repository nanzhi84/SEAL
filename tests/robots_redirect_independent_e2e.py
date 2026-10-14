"""Business boundary tests written before robots_redirect_urls implementation.
Failure modes: approved robots redirect fails; unapproved/near-match redirect
opens policy; policy-only URL leaks into normal crawl; Disallow ignored; unsafe
address or sensitive URL crosses network boundary. Real CLI/HTTP/Postgres/S3.
Run: uv run --no-sync python tests/robots_redirect_independent_e2e.py
"""
import json
import sys
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import yaml
from issue20_lab import Lab, ROOT, digest, write

class PolicyServer:
    def __init__(self, host="127.0.0.1"):
        self.requests=[];self.redirect=None;self.links=[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                owner.requests.append(self.path)
                if self.path=='/robots.txt':
                    status=302;body=b'';kind='text/plain'
                elif self.path.startswith('/policy'):
                    status=200;body=b'User-agent: *\nDisallow: /content/denied\n';kind='text/plain'
                else:
                    status=200;kind='text/html';body=('<html><head><title>Accepted content</title></head><body><main><h1>Accepted content</h1><p>Original readable business content survives redirected policy.</p>'+''.join('<a href="'+x+'">Target</a>' for x in owner.links)+'</main></body></html>').encode()
                self.send_response(status)
                if status==302:self.send_header('Location',owner.redirect)
                self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
        server_type=ThreadingHTTPServer
        if ':' in host:
            class IPv6Server(ThreadingHTTPServer): address_family=socket.AF_INET6
            server_type=IPv6Server
        self.server=server_type((host,0),Handler)
        url_host='['+host+']' if ':' in host else host
        self.base=f'http://{url_host}:{self.server.server_port}'
        self.cross=self.base
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def close(self): self.server.shutdown();self.server.server_close()

class Acceptance(Lab):
    def run_case(self,ident,server,target,approved,success,env=None):
        server.redirect=target;server.requests.clear();peer.requests.clear()
        server.links=[target]
        path=self.config(ident,['/content/allowed'],depth=1,robots=True)
        data=yaml.safe_load(path.read_text());data['entry_urls']=[server.base+'/content/allowed',server.base+'/content/denied'];data['allowed_path_prefixes']=['/content/'];data['robots_redirect_urls']=approved
        path.write_text(yaml.safe_dump(data));(self.artifacts/path.name).write_text(path.read_text())
        self.cli('collect',path,'--no-progress',expected=1 if success else (1,2),env=env)
        versions=self.rows('version',ident);observations=self.rows('observation',ident)
        self.check(ident+' approved policy enables allowed content' if success else ident+' rejected policy creates no Version',len(versions)==1 if success else not versions)
        self.check(ident+' Disallow prevents denied target request','/content/denied' not in server.requests)
        if success:
            self.check(ident+' ordinary crawl cannot request policy-only URL',server.requests.count('/policy')+peer.requests.count('/policy')==1,origin_requests=list(server.requests),peer_requests=list(peer.requests))
            self.check(ident+' allowed content crosses real HTTP and S3', '/content/allowed' in server.requests and bool(self.body(versions[0]['result_ref'])) if versions else False)
        else:
            self.check(ident+' rejected policy blocks content before HTTP','/content/allowed' not in server.requests)
            self.check(ident+' rejected redirect target never reaches fixture',not any(p.startswith('/policy') for p in server.requests+peer.requests))
        write(self.artifacts/(ident+'-evidence.json'),dict(observations=observations,versions=versions,requests=list(server.requests),peer_requests=list(peer.requests)))

lab=Acceptance();lab.artifacts=ROOT/'artifacts/robots-redirect-independent-20261014';lab.artifacts.mkdir(parents=True,exist_ok=True)
origin=PolicyServer();peer=PolicyServer("::1")
try:
    lab.start();lab.cli('init')
    lab.run_case('dd-017',origin,origin.base+'/policy',[origin.base+'/policy'],True)
    lab.run_case('dd-002',origin,peer.cross+'/policy',[peer.cross+'/policy'],True)
    lab.run_case('dd-003',origin,peer.cross+'/policy',[],False)
    lab.run_case('dd-004',origin,peer.cross+'/policy?unapproved=1',[peer.cross+'/policy'],False)
    for ident,target in [('dd-005','http://169.254.169.254/latest/meta-data/'),('dd-006',peer.cross.replace('http://','http://synthetic:dummy@')+'/policy')]:
        lab.run_case(ident,origin,target,[target],False)
    # Loopback fixture is explicitly authorized only in normal cases. Removing
    # authorization must still block before HTTP even with policy allowlisting.
    origin.requests.clear()
    lab.run_case('dd-007',origin,peer.cross+'/policy',[peer.cross+'/policy'],False,env={**lab.env,'SEAL_ALLOW_LOOPBACK':'0'})
    lab.check('without loopback exception no policy or content reaches fixture',not origin.requests and not peer.requests)
except Exception as error:
    lab.check('acceptance completes',False,error=lab.clean(str(error)))
finally:
    origin.close();peer.close();lab.close()
    for name in ('summary.json','environment.json'):
        path=lab.artifacts/name
        if path.exists():
            data=json.loads(path.read_text());data['command']='uv run --no-sync python tests/robots_redirect_independent_e2e.py'
            if name=='environment.json':data['test_sha256']['tests/robots_redirect_independent_e2e.py']=digest(Path(__file__).read_bytes())
            write(path,data)
sys.exit(any(c['status']=='FAIL' for c in lab.checks))
