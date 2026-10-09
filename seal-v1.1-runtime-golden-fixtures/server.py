#!/usr/bin/env python3
"""Pure stdlib local fixture HTTP server for SEAL V1.1 smoke tests. Loopback only."""
import argparse
import gzip
import json
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent / 'fixtures'
ROUTES = {
    '/notice/simple': 'notice_simple.html',
    '/notice/alternate': 'notice_alternate.html',
    '/notice/date-trap': 'notice_date_trap.html',
    '/notice/table': 'notice_table.html',
    '/notice/missing-optional': 'notice_missing_optional.html',
    '/notice/unicode': 'notice_unicode.html',
    '/notice/missing-title': 'notice_missing_title.html',
    '/list/1': 'list_1.html',
    '/list/2': 'list_2.html',
}
STATE = {'revision': 'A', 'flaky_attempts': 0, 'requests': Counter()}
LOCK = Lock()

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def reply(self, code, body, content_type='text/html; charset=utf-8', headers=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        path = u.path
        args = parse_qs(u.query)
        with LOCK:
            STATE['requests'][path] += 1
            if path == '/__control/reset':
                STATE['revision'] = 'A'
                STATE['flaky_attempts'] = 0
                STATE['requests'].clear()
                return self.reply(200, b'{"ok":true}', 'application/json')
            if path == '/__control/revision':
                value = args.get('value', [''])[0]
                if value not in ('A', 'B'):
                    return self.reply(400, b'{"error":"value must be A or B"}', 'application/json')
                STATE['revision'] = value
                return self.reply(200, json.dumps({'revision':value}).encode(), 'application/json')
            if path == '/__control/stats':
                data = {'revision':STATE['revision'], 'flaky_attempts':STATE['flaky_attempts'], 'requests':dict(STATE['requests'])}
                return self.reply(200, json.dumps(data, sort_keys=True).encode(), 'application/json')
            if path == '/flaky':
                STATE['flaky_attempts'] += 1
                first = STATE['flaky_attempts'] == 1
            else:
                first = False
            revision = STATE['revision']
        if path == '/redirect':
            return self.reply(302, b'', headers={'Location':'/notice/simple'})
        if path == '/empty':
            return self.reply(200, b'')
        if path == '/flaky' and first:
            return self.reply(503, b'Temporary error', 'text/plain; charset=utf-8', {'Retry-After':'0'})
        if path == '/flaky':
            path = '/notice/simple'
        if path == '/compressed':
            body = (ROOT / 'notice_simple.html').read_bytes()
            return self.reply(200, gzip.compress(body, mtime=0), headers={'Content-Encoding':'gzip'})
        if path == '/changing':
            name = 'notice_change_a.html' if revision == 'A' else 'notice_change_b.html'
        else:
            name = ROUTES.get(path)
        if name is None:
            return self.reply(404, b'Not found', 'text/plain; charset=utf-8')
        return self.reply(200, (ROOT / name).read_bytes())

    def log_message(self, format, *args):
        print('%s %s' % (self.address_string(), format % args), flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    print(f'SEAL fixture server listening on http://127.0.0.1:{args.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
