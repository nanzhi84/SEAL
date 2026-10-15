"""Independent real HTTP timeout contract regression, declared before adapter fix.
uv run --no-sync python tests/dd492_timeout_e2e.py
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import yaml
from dd492_pdf_acceptance import Acceptance,pdf

x=Acceptance()
body=pdf(1)
class Delayed(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def do_GET(self):
        time.sleep(35 if self.path=='/slow35.pdf' else 5)
        try:
            self.send_response(200);self.send_header('Content-Type','application/pdf');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Delayed)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    for filename,budget in [('slow35.pdf',45000),('slow5.pdf',2000)]:
        url=f'http://127.0.0.1:{server.server_port}/{filename}'
        cfg={'id':'dd-492','entry_urls':[url],'allowed_hosts':['127.0.0.1'],'host_path_scopes':{'127.0.0.1':['/']},'archive_approved':True,'scope':'isolated real HTTP timeout contract','robots':False,'concurrency':1,'budget':{'requests':5,'seconds':90,'response_bytes':10485760},'crawl4ai':{'mode':'http','max_depth':0,'page_timeout_ms':budget,'word_count_threshold':1}}
        path=x.work/(filename+'.yaml');path.write_text(yaml.safe_dump(cfg));x.dump(filename+'.config.json',cfg)
        start=time.monotonic();r=x.command(filename,['seal','collect',str(path),'--no-progress'],timeout=75);elapsed=time.monotonic()-start
        row=[r for r in x.rows() if r['url']==url][-1];x.dump(filename+'.observation.json',row)
        if budget==45000:
            x.check('35second delayed PDF succeeds within explicit45second budget',row['outcome']=='parsed' and row['sequence']==1 and bool(row['archive_ref']),elapsed_seconds=round(elapsed,3),outcome=row['outcome'],error=row['error'])
            if row['sequence']:
                x.check('delayed successful PDF preserves original and text',x.object(row['body_hash'])==body and 'INDEPENDENT_PAGE_001_COMPLETE_TEXT' in x.text(x.envelope(row)))
        else:
            x.check('2second budget explicitly fails before5second PDF delivery',row['outcome']=='network_failed' and not row['archive_ref'] and row['version_id'] is None and elapsed<5,elapsed_seconds=round(elapsed,3),outcome=row['outcome'],error=row['error'])
finally:
    server.shutdown();server.server_close()
failed=x.finish();print('TIMEOUT_E2E_ARTIFACT='+str(x.out));sys.exit(failed)
