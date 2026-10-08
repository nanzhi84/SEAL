"""Transport meter for the six-call cloud fixture campaign, not an HTTP client.

Wrap the installed SDK's real requests.Session.send: no response mocks, no SDK
replacement, no retries/redirects. Never persist headers, credentials, or raw
account identifiers. Only the two approved API paths may leave this process.
"""
import re
import time
from urllib.parse import urlparse

import requests

from .common import now_iso, sha256_hex, write_json


class APIMeter:
    def __init__(self, root, secret):
        self.root, self.secret = root, secret
        self.events = []
        self.last_finished = 0
        self.original = requests.sessions.Session.send

    def __enter__(self):
        meter = self

        def send(session, request, **kwargs):
            parsed = urlparse(request.url)
            route = (request.method, parsed.path)
            allowed = {('GET', '/v2/team/credit-usage'): 2, ('POST', '/v2/parse'): 4}
            if (parsed.scheme != 'https' or parsed.netloc != 'api.firecrawl.dev' or
                    parsed.query or route not in allowed):
                raise RuntimeError('API_ORIGIN_OR_OPERATION_NOT_AUTHORIZED')
            if len(meter.events) >= 6 or sum((e['method'], e['path']) == route
                                           for e in meter.events) >= allowed[route]:
                raise RuntimeError('HTTP_BUDGET_BLOCKED_BEFORE_DISPATCH')
            time.sleep(max(0, 3 - (time.monotonic() - meter.last_finished)))
            record = {'at': now_iso(), 'method': request.method, 'path': parsed.path,
                      'status': 'DISPATCH_RESERVED', 'http_status': None}
            meter.events.append(record)
            write_json(meter.root / 'http_ledger.json', meter.events)
            started = time.perf_counter()
            try:
                kwargs['allow_redirects'] = False
                response = meter.original(session, request, **kwargs)
                record.update({'http_status': response.status_code, 'status': 'RESPONSE',
                               'response_bytes': len(response.content),
                               'response_sha256': sha256_hex(response.content)})
                if request.method == 'POST':
                    # Fixed synthetic input only; redact even an unexpected token echo.
                    safe = response.text.replace(meter.secret, '[REDACTED]')
                    safe = re.sub(r'fc-[A-Za-z0-9_-]{20,}', '[REDACTED]', safe)
                    filename = f'http_response_{len(meter.events):02d}.json'
                    (meter.root / filename).write_text(safe)
                    record['sanitized_response_artifact'] = filename
                if 300 <= response.status_code < 400:
                    raise RuntimeError('REDIRECT_BLOCKED_NO_FOLLOW')
                return response
            except Exception as exc:
                record['status'] = 'FAILED'
                record['error_type'] = type(exc).__name__
                raise
            finally:
                record['elapsed_s'] = time.perf_counter() - started
                meter.last_finished = time.monotonic()
                write_json(meter.root / 'http_ledger.json', meter.events)

        requests.sessions.Session.send = send
        return self

    def __exit__(self, *args):
        requests.sessions.Session.send = self.original
        self.secret = None
