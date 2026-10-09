#!/usr/bin/env python3
"""Verify the fixture pack, not the SEAL repository runtime."""
import gzip
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

BASE=Path(__file__).resolve().parent
pack=json.loads((BASE / 'golden_expected.json').read_text(encoding='utf-8'))
assert len(pack['cases']) == 8
for item in pack['cases']:
    blob=(BASE / 'fixtures' / item['fixture']).read_bytes()
    assert hashlib.sha256(blob).hexdigest()==item['fixture_sha256'], item['id']
print('PASS: 8 fixed golden content hashes')
with socket.socket() as s:
    s.bind(('127.0.0.1', 0))
    port=s.getsockname()[1]
p=subprocess.Popen([sys.executable, str(BASE/'server.py'), '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
base=f'http://127.0.0.1:{port}'
try:
    for _ in range(60):
        try:
            urlopen(base+'/__control/reset', timeout=0.3).read()
            break
        except (URLError, OSError):
            time.sleep(0.05)
    else: raise RuntimeError('fixture server did not start')
    def get(path): return urlopen(base+path, timeout=3).read()
    assert get('/notice/simple')==(BASE/'fixtures/notice_simple.html').read_bytes()
    assert get('/changing')==(BASE/'fixtures/notice_change_a.html').read_bytes()
    get('/__control/revision?value=B')
    assert get('/changing')==(BASE/'fixtures/notice_change_b.html').read_bytes()
    get('/__control/revision?value=A')
    assert get('/changing')==(BASE/'fixtures/notice_change_a.html').read_bytes()
    print('PASS: A→B→A state transitions')
    # urllib automatically decompresses neither gzip nor content encoding for normal requests.
    compressed=get('/compressed')
    assert gzip.decompress(compressed)==(BASE/'fixtures/notice_simple.html').read_bytes()
    print('PASS: deterministic gzip response')
    get('/__control/reset')
    try: get('/flaky')
    except HTTPError as e: assert e.code==503
    else: raise AssertionError('first flaky response should be 503')
    assert get('/flaky')==(BASE/'fixtures/notice_simple.html').read_bytes()
    print('PASS: flaky 503 then 200')
    assert len(json.loads(get('/__control/stats'))['requests'])>0
    assert len(json.loads((BASE/'runtime_cases.json').read_text(encoding='utf-8'))['cases'])==12
    print('PASS: 12 runtime scenario specifications')
finally:
    p.terminate()
    try:p.wait(timeout=3)
    except subprocess.TimeoutExpired:p.kill();p.wait()
