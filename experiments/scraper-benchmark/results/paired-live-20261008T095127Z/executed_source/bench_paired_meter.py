"""Serial, pre-dispatch client/API budget ledger for one approved paired run."""
import json
import re
import time
from urllib.parse import urlparse

import requests

from .common import now_iso, sha256_hex, write_json


class Meter:
    def __init__(self, root, secret, urls):
        self.root, self.secret, self.urls = root, secret, set(urls)
        self.data = {'requests': [], 'credits_reserved': 0, 'request_cap': 80, 'credit_cap': 30,
                     'scope': 'client HTTP/API; provider-origin subrequests UNKNOWN'}
        self.delay, self.last_finish = 3.01, 0
        self.context = ('unconfigured', None, 0)
        self.original = requests.sessions.Session.send
        (root / 'http').mkdir()
        self.flush()

    def flush(self):
        write_json(self.root / 'ledger.json', self.data)

    def reserve(self, engine, endpoint, target_url=None, credits=0):
        if len(self.data['requests']) >= 80 or self.data['credits_reserved'] + credits > 30:
            raise RuntimeError('BUDGET_BLOCKED_BEFORE_DISPATCH')
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_finish)))
        item = {'id': len(self.data['requests']) + 1, 'engine': engine, 'endpoint': endpoint,
                'target_url': target_url, 'started_at': now_iso(), 'status': 'DISPATCH_RESERVED',
                'credits_reserved': credits}
        self.data['requests'].append(item)
        self.data['credits_reserved'] += credits
        self.flush()
        return item, time.perf_counter()

    def finish(self, item, started, **values):
        item.update(values, elapsed_s=time.perf_counter()-started, finished_at=now_iso())
        self.last_finish = time.monotonic()
        self.flush()

    def __enter__(self):
        meter = self
        def send(session, request, **kwargs):
            engine, target, credits = meter.context
            parsed = urlparse(request.url)
            if engine == 'firecrawl_cloud':
                if parsed.netloc != 'api.firecrawl.dev' or parsed.scheme != 'https':
                    raise RuntimeError('UNAPPROVED_API_ORIGIN')
                if (request.method, parsed.path) not in {
                        ('GET', '/v2/team/credit-usage'), ('POST', '/v2/scrape')}:
                    raise RuntimeError('UNAPPROVED_API_OPERATION')
                if request.method == 'POST':
                    payload = json.loads(request.body)
                    if payload.get('url') != target or target not in meter.urls:
                        raise RuntimeError('UNAPPROVED_TARGET_URL')
                    if payload.get('maxAge') != 0 or payload.get('proxy') != 'basic':
                        raise RuntimeError('UNAPPROVED_CACHE_OR_PROXY_SETTINGS')
            elif engine == 'robots':
                if request.method != 'GET' or request.url != target:
                    raise RuntimeError('UNAPPROVED_ROBOTS_DISPATCH')
            else:
                raise RuntimeError('UNMETERED_NETWORK_ATTEMPT')
            item, started = meter.reserve(engine, request.url, target, credits)
            try:
                kwargs['allow_redirects'] = False
                response = meter.original(session, request, **kwargs)
                values = {'status': 'RESPONSE', 'http_status': response.status_code,
                          'bytes': len(response.content), 'response_sha256': sha256_hex(response.content)}
                if engine == 'firecrawl_cloud' and request.method == 'POST':
                    safe = response.text.replace(meter.secret, '[REDACTED]')
                    safe = re.sub(r'fc-[A-Za-z0-9_-]{20,}', '[REDACTED]', safe)
                    filename = f'http/{item["id"]:03d}.json'
                    (meter.root / filename).write_text(safe)
                    values['response_artifact'] = filename
                meter.finish(item, started, **values)
                if 300 <= response.status_code < 400 and engine == 'firecrawl_cloud':
                    raise RuntimeError('API_REDIRECT_BLOCKED')
                return response
            except Exception as exc:
                if item['status'] == 'DISPATCH_RESERVED':
                    meter.finish(item, started, status='TRANSPORT_ERROR', error_type=type(exc).__name__)
                raise
        requests.sessions.Session.send = send
        return self

    def __exit__(self, *_):
        requests.sessions.Session.send = self.original
        self.secret = None
