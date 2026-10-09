"""Bounded public GET reconnaissance, separate from the SEAL Runtime and prior goldens."""
import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from parsel import Selector
from protego import Protego
from scrapy.http import HtmlResponse

from seal.core import BODY_SECRET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'experiments/due-diligence'))
# Reuse the existing experiment's parser and canonical map IDs without duplicating them.
from classify import extract  # noqa: E402
from inventory import MAP, clean_url, inventory  # noqa: E402

UA = 'SEAL-SourceExperiment/1.0 (bounded public-source feasibility)'
LOCKS = defaultdict(threading.Lock)
LAST = defaultdict(float)
WRITE = threading.Lock()
DETAIL = re.compile(r'/t\d{6,}_|/content[/-]|/\d{6,}\.(?:s?html?|aspx)|/\d{4}/\d{2}/|/\d{6}/|detail|article|/news/[^/]+/?$|\.pdf(?:$|\?)', re.I)
TOPIC = re.compile(r'公告|公示|处罚|决定|通知|发布|案例|通报|关于|招标|采购|备案|名单|办法|规定|报告|news|notice|press', re.I)
IGNORE = re.compile(r'隐私|关于我们|联系我们|网站地图|版权|登录|注册|privacy|cookie|login', re.I)
SESSION = re.compile(rb'(?:jsessionid|PHPSESSID|ASP\.NET_SessionId|csrf[_-]?token)\s*[=:]\s*[\"\x27]?[^\s\"\x27<>;&]{8,}', re.I)


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Probe:
    def __init__(self, out):
        self.out = out
        self.old = json.loads((ROOT / 'experiments/due-diligence/golden/entries.json').read_text())
        self.denied = {r['url'] for r in self.old if r['outcome'] == 'ROBOTS_DENIED'}
        self.policies = {}
        for row in self.old:
            policy = row.get('robots', {})
            path = policy.get('robots_path')
            if path:
                file = ROOT / row['campaign'] / path
                if file.exists():
                    obs = policy.get('robots_observation', {})
                    u = urlsplit(obs.get('url', row['url']))
                    self.policies[f'{u.scheme}://{u.netloc}'] = Protego.parse(file.read_text())

    def request(self, url, kind):
        url = clean_url(url)
        chain = []
        for hop in range(5):
            p = urlsplit(url)
            policy = self.policies.get(f'{p.scheme}://{p.netloc}')
            if url in self.denied or (policy and not policy.can_fetch(url, UA)):
                return {'url': url, 'outcome': 'ROBOTS_DENIED', 'chain': chain}
            rec = {'url': url, 'kind': kind, 'at': now(), 'hop': hop}
            data = b''
            with LOCKS[p.hostname]:
                time.sleep(max(0, 1.5 - (time.monotonic() - LAST[p.hostname])))
                start = time.monotonic()
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                    wire_url = quote(url, safe=':/?&=%#@+;,!$()*[]~')
                    req = urllib.request.Request(wire_url, headers={'User-Agent': UA, 'Connection': 'close'})
                    try:
                        response = opener.open(req, timeout=12)
                    except urllib.error.HTTPError as exc:
                        response = exc
                    with response:
                        rec['status'] = response.code
                        rec['mime'] = response.headers.get('Content-Type', '')
                        location = response.headers.get('Location')
                        data = response.read(5242881)
                        if len(data) > 5242880:
                            rec['error'] = 'BODY_LIMIT'
                            data = b''
                        rec['bytes'] = len(data)
                        rec['sha256'] = hashlib.sha256(data).hexdigest()
                except Exception as exc:
                    rec['error'] = type(exc).__name__
                    rec['cause'] = type(getattr(exc, 'reason', None)).__name__
                    location = None
                LAST[p.hostname] = time.monotonic()
                rec['seconds'] = round(time.monotonic() - start, 3)
            with WRITE:
                with (self.out / 'requests.jsonl').open('a') as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            chain.append(rec)
            if rec.get('error'):
                return {**rec, 'outcome': 'NETWORK_ERROR', 'chain': chain}
            if rec['status'] in (301, 302, 303, 307, 308) and location:
                url = clean_url(urljoin(url, location))
                if urlsplit(url).scheme not in ('http', 'https'):
                    break
                continue
            response = HtmlResponse(url, body=data, status=rec['status'], headers={'Content-Type': rec['mime']})
            try:
                result = extract(response)
            except Exception as exc:
                result = {'outcome': 'PARSE_ERROR', 'reason': type(exc).__name__}
            result.update(url=url, chain=chain, sha256=rec['sha256'], at=rec['at'], status=rec['status'])
            if result['outcome'] in ('ACCESSIBLE_HTML', 'ACCESSIBLE_PDF', 'ACCESSIBLE_JSON', 'JS_SHELL', 'SKIP_INPUT'):
                if BODY_SECRET.search(data) or SESSION.search(data):
                    result['archive'] = 'sensitive_pattern_not_retained'
                else:
                    (self.out / 'bodies' / rec['sha256']).write_bytes(data)
                    result['raw_path'] = 'bodies/' + rec['sha256']
                if result.get('mime') != 'application/pdf' and result['outcome'] != 'ACCESSIBLE_JSON':
                    result.update(self.inspect(response))
            return result
        return {'url': url, 'outcome': 'REDIRECT_LIMIT', 'chain': chain}

    def inspect(self, response):
        sel = Selector(response.text)
        forms = []
        for form in sel.css('form'):
            fields = [{'name': n.attrib.get('name', n.attrib.get('id', '')), 'type': n.attrib.get('type', n.root.tag), 'hint': n.attrib.get('placeholder', '')[:70]} for n in form.css('input,select,textarea') if n.attrib.get('type', '') not in ('hidden', 'submit', 'button')]
            forms.append({'action': clean_url(response.urljoin(form.attrib.get('action') or response.url)), 'method': form.attrib.get('method', 'GET').upper(), 'fields': fields})
        links = []
        for a in sel.css('a[href]'):
            u = response.urljoin(a.attrib['href'])
            if not u.startswith(('http://', 'https://')):
                continue
            text = ' '.join(a.xpath('.//text()').getall()).strip()
            if len(text) < 9 or IGNORE.search(text) or urlsplit(u).hostname != urlsplit(response.url).hostname:
                continue
            score = 3 * bool(DETAIL.search(u)) + 2 * bool(TOPIC.search(text)) + bool(re.search(r'20\d{2}', u))
            if score >= 3:
                links.append({'text': text[:180], 'url': clean_url(u), 'score': score})
        links.sort(key=lambda x: -x['score'])
        pagination = []
        for a in sel.css('a[href]'):
            if re.fullmatch(r'\s*(下一页|下页|Next|Next page|›|>)\s*', ' '.join(a.xpath('.//text()').getall()), re.I):
                u = response.urljoin(a.attrib['href'])
                if urlsplit(u).hostname == urlsplit(response.url).hostname and u.startswith(('http://', 'https://')):
                    pagination.append(clean_url(u))
        blocks = []
        for selector in ('article', '.TRS_Editor', '.vF_detail_content', '#zoom', '#fontzoom', '.txt_txt', '#UCAP-CONTENT', '.pages_content', '.article-content', '.article_content', '.content-text', '.detail-content'):
            nodes = sel.css(selector)
            for node in nodes:
                text = ' '.join(t.strip() for t in node.xpath('.//text()[not(ancestor::script or ancestor::style)]').getall() if t.strip())
                if len(text) >= 100:
                    blocks.append({'selector': selector, 'chars': len(text), 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'excerpt': text[:250]})
        scripts = '\n'.join(sel.css('script:not([src])::text').getall())
        refs = re.findall(r'''["']([^"'\s]{1,200}(?:\.json|\.do|\.action|/api/[^"'\s]*)(?:\?[^"'\s]*)?)["']''', scripts)
        return {'forms': forms[:10], 'candidates': links[:12], 'pagination': list(dict.fromkeys(pagination))[:3], 'article_blocks': blocks[:3], 'api_hints': list(dict.fromkeys(clean_url(response.urljoin(u)) for u in refs))[:12]}

    def one(self, url, rows):
        if all(r['map_noise'] for r in rows):
            return {'ids': [r['id'] for r in rows], 'entry': {'outcome': 'MAP_NOISE', 'url': url}}
        entry = self.request(url, 'entry')
        result = {'ids': [r['id'] for r in rows], 'entry': entry}
        if entry.get('outcome') == 'ACCESSIBLE_HTML':
            choices = [c for c in entry.get('candidates', []) if c['url'] != entry['url']]
            if choices:
                result['detail_link'] = choices[0]
                result['detail'] = self.request(choices[0]['url'], 'detail')
            pages = [u for u in entry.get('pagination', []) if u != entry['url']]
            if pages:
                result['next_page'] = self.request(pages[0], 'pagination')
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'bodies').mkdir()
    rows = inventory()
    groups = defaultdict(list)
    for row in rows:
        groups[row['url']].append(row)
    save(args.output / 'inventory.json', rows)
    probe = Probe(args.output)
    manifest = {'started_at': now(), 'python': sys.version, 'map_sha256': hashlib.sha256(MAP.read_bytes()).hexdigest(), 'baseline': 'experiments/due-diligence/golden/entries.json', 'protocol': json.loads((Path(__file__).parent / 'protocol.json').read_text()), 'scope': 'fresh GET per unique URL except baseline robots-denied paths and footer noise; optional one detail and next page; classifications require separate offline review'}
    save(args.output / 'manifest.json', manifest)
    completed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(probe.one, url, group): group for url, group in groups.items()}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {'ids': [r['id'] for r in futures[future]], 'entry': {'outcome': 'LOCAL_ERROR', 'reason': type(exc).__name__}}
            completed.append(result)
            with (args.output / 'results.jsonl').open('a') as f:
                f.write(json.dumps(result, ensure_ascii=False) + '\n')
            if len(completed) % 25 == 0:
                print(json.dumps({'completed': len(completed), 'total': len(groups)}), flush=True)
    save(args.output / 'results.json', completed)
    manifest['finished_at'] = now()
    manifest['completed'] = len(completed)
    save(args.output / 'manifest.json', manifest)


if __name__ == '__main__':
    main()
