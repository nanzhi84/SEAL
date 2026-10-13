"""Focused real CLI E2E: overlapping recheck downloads with identical long filenames.

Failure modes: sequential requests mistaken for concurrency; same suggested name
overwrites another input; Unicode/long filenames break acquisition; parser re-fetch
changes input; file/result/Observation references cross between resources; process
restart cannot recover each file. No SDK result or product service is mocked.

Run: uv run --no-sync python tests/issue20_concurrency.py --prior artifacts/<run>
Requires the frozen dependency, Docker/MinIO/PostgreSQL setup used by issue20_lab.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

import yaml

from issue20_fixture import pdf
from issue20_lab import Lab, ROOT, digest, write


class ConcurrentAttachments(Lab):
    def execute(self, prior):
        command = 'uv run --no-sync python tests/issue20_concurrency.py'
        if prior:
            command += ' --prior ' + str(prior)
        self.command = command
        metadata = json.loads((self.artifacts / 'environment.json').read_text())
        metadata.update(command=command, scope='Actual overlapping recheck PDF HTTP inputs; identical RFC5987 filename; CLI→SQL→S3→new consumer process')
        write(self.artifacts / 'environment.json', metadata)
        if prior:
            previous = json.loads((prior / 'environment.json').read_text())
            self.check('focused E2E uses same product bytes as original126PASS run',
                       previous['source_sha256'] == metadata['source_sha256'])
            self.check('all original126PASS executable test files remain unchanged', all(
                (ROOT / path).exists() and digest((ROOT / path).read_bytes()) == sha
                for path, sha in previous['test_sha256'].items()))
        self.cli('init')
        inputs = {
            '/concurrent/alpha.pdf': dict(initial=pdf('Issue20 original Alpha seed'),
                body=pdf('Issue20 CONCURRENT_ALPHA input version two'), marker='CONCURRENT_ALPHA'),
            '/concurrent/beta.pdf': dict(initial=pdf('Issue20 original Beta seed'),
                body=pdf('Issue20 CONCURRENT_BETA input version two'), marker='CONCURRENT_BETA'),
        }
        for endpoint, item in inputs.items():
            self.origin.set(endpoint, item['initial'], 'application/pdf')
        path = self.config('dd-015', list(inputs))
        config = yaml.safe_load(path.read_text()); config['concurrency'] = 2
        path.write_text(yaml.safe_dump(config)); (self.artifacts / path.name).write_text(path.read_text())
        self.cli('collect', path, '--no-progress')
        initial = self.rows('observation', 'dd-015')
        self.check('sequential seed collect creates the two resources needed by public recheck',
                   len(initial) == 2 and all(o['version_id'] and o['archive_ref'] for o in initial))
        delay = 3.0
        filename = '尽职调查原始附件' * 35 + '.pdf'
        disposition = 'attachment; filename="same-original.pdf"; filename*=UTF-8\'\'' + quote(filename)
        self.check('server suggests identical Unicode filename exceeding normal filesystem basename limit',
                   len(filename.encode()) > 255 and len(disposition.encode()) < 8192,
                   utf8_filename_bytes=len(filename.encode()), header_bytes=len(disposition.encode()))
        first_request_index = len(self.origin.requests)
        for endpoint, item in inputs.items():
            self.origin.set(endpoint, item['body'], 'application/pdf',
                            headers={'Content-Disposition': disposition}, delay=delay)
            (self.artifacts / ('input-' + digest(item['body']) + '.pdf')).write_bytes(item['body'])
        _, receipt = self.cli('recheck', path, '--no-progress')
        run_id = receipt.get('run_id')
        self.check('public recheck returned actual Run identity', bool(run_id))
        requests = [r for r in self.origin.requests[first_request_index:] if r['path'] in inputs]
        self.check('same-input recheck performs exactly one actual request per PDF resource',
                   len(requests) == 2 and {r['path'] for r in requests} == set(inputs), requests=requests)
        overlap = abs(requests[0]['time'] - requests[1]['time']) if len(requests) == 2 else delay
        self.check('real HTTP requests overlap rather than sequential root collection',
                   overlap < delay and len(requests) == 2,
                   start_time_difference_seconds=overlap, server_response_delay_seconds=delay)
        observations = [o for o in self.rows('observation', 'dd-015') if str(o['run_id']) == run_id]
        self.check('overlapping downloads produce two separate successful resource observations',
                   len(observations) == 2 and len({o['resource_id'] for o in observations}) == 2
                   and all(o['version_id'] for o in observations))
        for endpoint, item in inputs.items():
            url = self.origin.base + endpoint
            observation = next(o for o in observations if o['url'] == url)
            key = digest(item['body']); envelope = json.loads(self.body(observation['result_ref']))
            headers = {k.lower(): v for k, v in (envelope['result'].get('response_headers') or {}).items()}
            markdown = envelope['result']['markdown']['raw_markdown']
            other = next(i['marker'] for e, i in inputs.items() if e != endpoint)
            self.check(endpoint + ' retains exactly its own requested original PDF bytes',
                       observation['archive_ref'] == key and observation['body_hash'] == key
                       and self.body(key) == item['body']
                       and next(r['sha256'] for r in requests if r['path'] == endpoint) == key)
            self.check(endpoint + ' actual result has the shared filename header and its own file reference',
                       headers.get('content-disposition') == disposition
                       and envelope['result'].get('downloaded_files') == [key]
                       and key in envelope.get('attachment_refs', []) and key in observation['attachment_refs'])
            self.check(endpoint + ' SDK parsed its own same-input PDF without cross contamination',
                       envelope['result']['success'] and envelope['result']['url'] == url
                       and item['marker'] in markdown and other not in markdown)
            version = self.sql('SELECT * FROM seal.version WHERE id=%s', (observation['version_id'],))[0]
            resource = self.sql('SELECT * FROM seal.resource WHERE id=%s', (observation['resource_id'],))[0]
            self.check(endpoint + ' concurrent Version and current pointer match its own Observation/input/result',
                       version['resource_id'] == observation['resource_id']
                       and version['observation_id'] == observation['id'] and version['body_hash'] == key
                       and version['result_ref'] == observation['result_ref'] and version['sequence'] == 2
                       and resource['current_version_id'] == version['id'])
            (self.artifacts / ('result-' + observation['result_ref'] + '.json')).write_bytes(
                self.body(observation['result_ref']))
        code = '''import json,os,sys,hashlib
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from seal.objects import Objects
from seal.archive import deserialize_result
with psycopg.connect(os.environ['SEAL_DATABASE_URL'],row_factory=dict_row) as c:
 rows=c.execute('SELECT * FROM seal.observation WHERE run_id=%s ORDER BY url',(sys.argv[1],)).fetchall()
out=[]
for o in rows:
 raw=Objects().get(o['archive_ref']); envelope=Objects().json(o['result_ref']);r=deserialize_result(envelope)
 files=[{'sha256':hashlib.sha256(Path(p).read_bytes()).hexdigest(),'hex':Path(p).read_bytes().hex()} for p in r.downloaded_files or []]
 out.append({'url':o['url'],'archive_ref':o['archive_ref'],'result_ref':o['result_ref'],
  'archive_hex':raw.hex(),'archive_sha256':hashlib.sha256(raw).hexdigest(),'files':files,
  'markdown':str(r.markdown),'success':r.success,'sdk_url':r.url})
print(json.dumps({'results':out}))'''
        _, rebuilt = self.process(['-c', code, run_id])
        result_rows = rebuilt.get('results', [])
        for endpoint, item in inputs.items():
            native = next(r for r in result_rows if r['url'] == self.origin.base + endpoint)
            self.check(endpoint + ' new process recovers each original and SDK downloaded file byte for byte',
                       bytes.fromhex(native['archive_hex']) == item['body']
                       and native['archive_sha256'] == digest(item['body'])
                       and len(native['files']) == 1
                       and bytes.fromhex(native['files'][0]['hex']) == item['body']
                       and native['files'][0]['sha256'] == digest(item['body'])
                       and native['success'] and item['marker'] in native['markdown'])
        write(self.artifacts / 'cross-process-results.json', rebuilt)
        write(self.artifacts / 'overlap-evidence.json', dict(delay=delay, start_difference=overlap,
            requests=requests, content_disposition=disposition, decoded_filename_utf8_bytes=len(filename.encode()),
            run_id=run_id, observations=observations, fixed_input_hashes={e:digest(i['body']) for e,i in inputs.items()}))
        write(self.artifacts / 'sql.json', self.snapshot())
        expected_files = {digest(i['body']) for i in inputs.values()} | {digest(i['initial']) for i in inputs.values()}
        inventory = []
        for key in self.objects():
            raw = self.body(key)
            if key in expected_files:
                kind = 'original_attachment'
            else:
                envelope = json.loads(raw)
                kind = 'CrawlResult' if isinstance(envelope.get('result'), dict) else 'unknown'
            inventory.append(dict(key=key, sha256=digest(raw), bytes=len(raw), category=kind))
        self.check('focused real bucket contains only verified native results and original inputs',
                   all(i['key'] == i['sha256'] and i['category'] in ('CrawlResult', 'original_attachment') for i in inventory))
        write(self.artifacts / 'objects.json', inventory)
        shutil.copyfile(self.origin.log, self.artifacts / 'http-requests.jsonl')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--prior', type=Path)
    args = parser.parse_args()
    lab = ConcurrentAttachments()
    try:
        lab.start(); lab.execute(args.prior.resolve() if args.prior else None)
    except Exception as exc:
        lab.check('focused concurrency E2E completed', False, category=type(exc).__name__, detail=lab.clean(str(exc)))
    finally:
        metadata_path = lab.artifacts / 'environment.json'
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text())
            lab.check('focused E2E begins and ends with identical product bytes', all(
                (ROOT / path).exists() and digest((ROOT / path).read_bytes()) == sha
                for path, sha in metadata['source_sha256'].items()))
            lab.check('focused E2E begins and ends with identical executable test bytes', all(
                (ROOT / path).exists() and digest((ROOT / path).read_bytes()) == sha
                for path, sha in metadata['test_sha256'].items()))
        lab.close()
        summary = json.loads((lab.artifacts / 'summary.json').read_text())
        summary.update(command=getattr(lab, 'command', 'uv run --no-sync python tests/issue20_concurrency.py'),
                       unverified=['Public website concurrency; production; browser-triggered downloads'])
        write(lab.artifacts / 'summary.json', summary)
    raise SystemExit(int(any(c['status'] == 'FAIL' for c in lab.checks)))
