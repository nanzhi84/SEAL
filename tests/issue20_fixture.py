"""Issue 20 isolated HTTP origin; response bytes are independent acceptance inputs."""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def html(title, text, links=()):
    return ('<!doctype html><html><head><title>' + title + '</title></head><body><main>'
            '<h1>' + title + '</h1><p>' + text + '</p>' + ''.join(
                '<a href="' + link + '">' + link + '</a>' for link in links)
            + '</main></body></html>').encode()


def pdf(text):
    stream = f'BT /F1 16 Tf 72 720 Td ({text}) Tj ET'.encode()
    entries = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
               b'/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream
               + b'\nendstream']
    raw = bytearray(b'%PDF-1.4\n'); offsets = [0]
    for n, entry in enumerate(entries, 1):
        offsets.append(len(raw)); raw.extend(f'{n} 0 obj\n'.encode() + entry + b'\nendobj\n')
    xref = len(raw); raw.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        raw.extend(f'{offset:010d} 00000 n \n'.encode())
    raw.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(raw)


def long_pdf(pages=1800):
    """Valid many-page text input widens real PDF scrape window before SDK delivery."""
    children = ' '.join(f'{4 + 2 * i} 0 R' for i in range(pages))
    entries = [b'<< /Type /Catalog /Pages 2 0 R >>',
               f'<< /Type /Pages /Kids [{children}] /Count {pages} >>'.encode(),
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for i in range(pages):
        stream = f'BT /F1 16 Tf 72 720 Td (Issue20 retained original PDF page {i}) Tj ET'.encode()
        entries.extend([f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
            f'/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>'.encode(),
            b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream'])
    raw = bytearray(b'%PDF-1.4\n'); offsets = [0]
    for n, entry in enumerate(entries, 1):
        offsets.append(len(raw)); raw.extend(f'{n} 0 obj\n'.encode() + entry + b'\nendobj\n')
    xref = len(raw); raw.extend(f'xref\n0 {len(entries) + 1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        raw.extend(f'{offset:010d} 00000 n \n'.encode())
    raw.extend(f'trailer\n<< /Size {len(entries) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(raw)


class Origin:
    def __init__(self, work):
        self.log = work / 'http-requests.jsonl'
        self.requests, self.responses, self.original_files, self.lock = [], {}, {}, threading.Lock()
        self.alpha = html('Archive contract Alpha', 'Stable Alpha article body.')
        self.beta = html('Archive contract Beta', 'Changed Beta article body.')
        self.pdf_a = pdf('Issue 20 same input PDF Alpha')
        self.pdf_b = pdf('Issue 20 forbidden second request Beta')
        self.set('/article', self.alpha)
        self.set('/source.pdf', self.pdf_a, 'application/pdf', next_body=self.pdf_b)
        self.set('/unsupported.xlsx', b'Issue20 original unsupported XLSX bytes',
                 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.set('/network', b'not delivered', status='network')
        self.set('/robots.txt', b'User-agent: *\nDisallow: /denied\n', 'text/plain')
        self.set('/denied', html('Denied', 'Never request this policy denied article.'))
        self.set('/slow', html('Slow', 'Delivered only after the interrupt window.'), delay=12)
        self.set('/before', html('Before', 'Committed before the interrupted sibling.'))
        self.set('/index', html('Index', 'Parallel HTML and PDF resources.',
                               ('/article', '/source.pdf', '/unsupported.xlsx')))
        self.set('/recovery', html('Recovery', 'Recovery entry.', ('/before', '/slow')))
        self.set('/download-one.bin', b'Issue20 downloaded attachment one',
                 'application/octet-stream', headers={'Content-Disposition': 'attachment; filename="one.bin"'})
        self.set('/download-two.bin', b'Issue20 downloaded attachment two',
                 'application/octet-stream', headers={'Content-Disposition': 'attachment; filename="two.bin"'})
        self.set('/dom', (
            '<html><head><title>DOM archive contract</title></head><body><main>'
            '<h1>Actual JavaScript DOM</h1><div id="target">Initial HTML</div>'
            '<p>Browser input must retain rendered evidence and original downloaded files.</p>'
            '</main><script>document.querySelector("#target").textContent="Rendered Alpha";'
            'for (const [i,url] of ["/download-one.bin","/download-two.bin"].entries()) {'
            'setTimeout(()=>{let a=document.createElement("a");a.href=url;a.download="";'
            'document.body.appendChild(a);a.click();},100+i*300);}</script></body></html>'
        ).encode())
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                with origin.lock:
                    response = dict(origin.responses.get(self.path, dict(body=b'missing',
                                    mime='text/plain', status=404, headers={}, delay=0)))
                    n = sum(r['path'] == self.path for r in origin.requests)
                    if n and response.get('next_body') is not None:
                        response['body'] = response['next_body']
                    body = response['body']
                    record = {'path': self.path, 'ordinal': n + 1, 'time': time.time(),
                              'status': response['status'], 'mime': response['mime'],
                              'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                    origin.requests.append(record)
                    with origin.log.open('a') as stream:
                        stream.write(json.dumps(record) + '\n')
                time.sleep(response['delay'])
                if response['status'] == 'network':
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                self.send_response_only(response['status'])
                self.send_header('Content-Type', response['mime'])
                self.send_header('Content-Length', str(len(body)))
                for key, value in response['headers'].items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def set(self, path, body, mime='text/html', status=200, headers=None, delay=0, next_body=None):
        self.responses[path] = dict(body=body, mime=mime, status=status, headers=headers or {},
                                    delay=delay, next_body=next_body)
        if mime not in ('text/html', 'text/plain'):
            self.original_files[hashlib.sha256(body).hexdigest()] = body

    def count(self, path):
        with self.lock:
            return sum(r['path'] == path for r in self.requests)

    def wait(self, path, n=1):
        deadline = time.monotonic() + 45
        while self.count(path) < n:
            if time.monotonic() > deadline:
                raise TimeoutError(f'No observed real request for {path}')
            time.sleep(.1)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown(); self.server.server_close()
