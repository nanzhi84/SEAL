"""Issue #20 independent business E2E.

Failure modes before implementation: duplicate HTML/DOM/manifest/page artifacts;
lost or invented SDK results; failed/unsupported Version advancement; missing
original attachment; PDF fetched twice with different input; lost body comparison;
unchanged overwrites original Version result; A→B→A collapsed; recovery duplicates
delivery or loses already committed results; empty-schema upgrade/init drift;
S3 corruption accepted or DB reference committed before verified object.

Run: uv run --no-sync python tests/issue20_e2e.py
Requires frozen Python 3.12 dependencies, installed Chromium, Docker, pg_dump,
pg_restore and local postgres:17-alpine / seal-minio / fixed mc images.
No product code or SDK result is mocked. No old-content migration is tested.
"""
from __future__ import annotations

import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import time

from issue20_fixture import html
from issue20_lab import Lab, ROOT, digest, write

BASELINE = '3215759176db9240bdb0bf0b0fa6a12512c801fc'


class Acceptance(Lab):
    def initialization(self):
        before = self.sql('SELECT table_schema,table_name FROM information_schema.tables ORDER BY 1,2')
        self.cli('init', env={**self.env, 'SEAL_S3_ENDPOINT': 'http://127.0.0.1:1'}, expected=(1, 2))
        self.check('unavailable S3 init creates no database schema', before == self.sql(
            'SELECT table_schema,table_name FROM information_schema.tables ORDER BY 1,2'))
        self.cli('init')
        stable = self.snapshot(); ledger = self.sql('SELECT * FROM seal.migration ORDER BY name')
        self.cli('init')
        self.check('new empty database repeated init preserves schema ledger and rows',
                   stable == self.snapshot() and ledger == self.sql('SELECT * FROM seal.migration ORDER BY name'))
        self.check('new init installs Issue20 storage schema', any(r['name'].startswith('0012') for r in ledger))
        self.sql('CREATE DATABASE issue20_empty_upgrade')
        old_dsn = self.dsn.rsplit('/', 1)[0] + '/issue20_empty_upgrade'
        checkout = self.work / 'baseline'; checkout.mkdir()
        raw = subprocess.check_output(['git', 'archive', BASELINE, 'src/seal'], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            archive.extractall(checkout, filter='data')
        old_env = {**self.env, 'SEAL_DATABASE_URL': old_dsn, 'PYTHONPATH': str(checkout / 'src')}
        self.cli('init', env=old_env, label='actual pre-Issue20 CLI initializes isolated empty old schema')
        old_ledger = self.sql('SELECT * FROM seal.migration ORDER BY name', dsn=old_dsn)
        self.check('baseline empty schema is through0011 without0012',
                   any(r['name'].startswith('0011') for r in old_ledger)
                   and not any(r['name'].startswith('0012') for r in old_ledger))
        backup = self.work / 'empty-schema-before-upgrade.dump'
        upgraded_env = {**self.env, 'SEAL_DATABASE_URL': old_dsn}
        self.cli('init', '--backup', backup, env=upgraded_env)
        upgraded = self.sql('SELECT * FROM seal.migration ORDER BY name', dsn=old_dsn)
        self.check('actual empty schema upgrade retains checksums and applies0012',
                   all(next(r for r in upgraded if r['name'] == old['name'])['checksum'] == old['checksum']
                       for old in old_ledger) and any(r['name'].startswith('0012') for r in upgraded))
        self.cli('init', env=upgraded_env)
        self.check('empty upgraded database repeated init preserves migration rows',
                   upgraded == self.sql('SELECT * FROM seal.migration ORDER BY name', dsn=old_dsn))
        self.check('upgrade backup is a real readable pg_dump archive',
                   backup.exists() and subprocess.run(['pg_restore', '--list', str(backup)],
                   capture_output=True).returncode == 0)
        write(self.artifacts / 'schema-upgrade.json', dict(baseline=BASELINE,
            old_migrations=old_ledger, new_migrations=upgraded,
            backup_sha256=digest(backup.read_bytes()) if backup.exists() else None))

    def timeline(self):
        self.origin.set('/article', self.origin.alpha, headers={'Set-Cookie': 'synthetic_cookie=issue20_marker'})
        path = self.config('dd-017', ['/article'])
        self.cli('collect', path, '--no-progress')
        initial = self.rows('version', 'dd-017'); obs = self.rows('observation', 'dd-017')
        self.check('HTML initial real parse creates one successful Version', len(initial) == 1)
        if not initial:
            return
        first = initial[0]
        self.check('HTML input fingerprint is retained without independent archive',
                   obs[-1]['body_hash'] == digest(self.origin.alpha) and obs[-1]['archive_ref'] is None
                   and obs[-1]['body_hash'] not in self.objects() and obs[-1]['result_ref'])
        self.cli('recheck', path, '--no-progress')
        unchanged = self.rows('observation', 'dd-017')[-1]
        self.check('unchanged records own result and keeps originating Version',
                   unchanged['outcome'] == 'unchanged' and bool(unchanged['result_ref'])
                   and self.rows('version', 'dd-017') == initial)
        self.check('each unchanged observation references its own readable real SDK result',
                   unchanged['result_ref'] in self.objects())
        self.origin.set('/article', self.origin.alpha, headers={'X-Result-Edition': 'changed-result'})
        self.cli('recheck', path, '--no-progress')
        changed_result = self.rows('observation', 'dd-017')[-1]
        self.check('unchanged input with new result retains current Observation result only',
                   changed_result['outcome'] == 'unchanged'
                   and changed_result['result_ref'] != first['result_ref']
                   and self.rows('version', 'dd-017') == initial)
        self.origin.set('/article', self.origin.beta)
        self.cli('recheck', path, '--no-progress')
        self.origin.set('/article', self.origin.alpha)
        self.cli('recheck', path, '--no-progress')
        timeline = self.rows('version', 'dd-017')
        self.check('A→B→A produces contiguous three Versions using current successful input',
                   [v['sequence'] for v in timeline] == [1, 2, 3]
                   and [v['body_hash'] for v in timeline] == [digest(self.origin.alpha),
                        digest(self.origin.beta), digest(self.origin.alpha)])
        self.origin.set('/article', html('Failure', 'Actual failing response body.'), status=500)
        self.cli('recheck', path, '--no-progress', expected=1)
        failed = self.rows('observation', 'dd-017')[-1]
        self.check('actual failing SDK response is retained without success Version advancement',
                   bool(failed['result_ref']) and bool(failed['error'])
                   and failed['archive_ref'] is None and self.rows('version', 'dd-017') == timeline)
        self.check('all Versions retain exact result reference of originating Observation', not self.sql(
            'SELECT v.id FROM seal.version v JOIN seal.observation o ON o.id=v.observation_id '
            'WHERE v.result_ref IS DISTINCT FROM o.result_ref'))

    def mixed_resources(self):
        path = self.config('dd-002', ['/index'], depth=1)
        self.cli('collect', path, '--no-progress', expected=1)
        observations = self.rows('observation', 'dd-002')
        pdf_obs = next((r for r in observations if r['url'].endswith('/source.pdf')), None)
        xlsx_obs = next((r for r in observations if r['url'].endswith('/unsupported.xlsx')), None)
        self.check('HTML and original PDF remain independent sibling resources',
                   pdf_obs is not None and any(r['url'].endswith('/index') for r in observations)
                   and pdf_obs['resource_id'] != next(r['resource_id'] for r in observations if r['url'].endswith('/index')))
        self.check('PDF downloaded once and parsed from exact same captured bytes',
                   pdf_obs is not None and self.origin.count('/source.pdf') == 1
                   and pdf_obs['body_hash'] == digest(self.origin.pdf_a)
                   and self.body(pdf_obs['archive_ref']) == self.origin.pdf_a and bool(pdf_obs['version_id']))
        self.check('unsupported downloaded original retained with real failed result and no Version',
                   xlsx_obs is not None and xlsx_obs['outcome'] in ('unsupported', 'parse_failed')
                   and bool(xlsx_obs['result_ref']) and bool(xlsx_obs['archive_ref'])
                   and self.body(xlsx_obs['archive_ref']) == self.origin.responses['/unsupported.xlsx']['body']
                   and xlsx_obs['version_id'] is None)
        self.origin.set('/parallel-one.pdf', self.origin.pdf_a, 'application/pdf')
        self.origin.set('/parallel-two.pdf', self.origin.pdf_b, 'application/pdf')
        path = self.config('dd-010', ['/parallel-one.pdf', '/parallel-two.pdf'])
        import yaml
        config = yaml.safe_load(path.read_text()); config['concurrency'] = 2
        path.write_text(yaml.safe_dump(config)); (self.artifacts / path.name).write_text(path.read_text())
        self.cli('collect', path, '--no-progress')
        parallel = self.rows('observation', 'dd-010')
        self.check('concurrent attachment resources cannot mix original file references',
                   len(parallel) == 2 and all(self.body(r['archive_ref']) == (
                       self.origin.pdf_a if r['url'].endswith('one.pdf') else self.origin.pdf_b) for r in parallel))

    def policy_network(self):
        policy = self.config('dd-003', ['/denied'], robots=True)
        self.cli('collect', policy, '--no-progress', expected=1)
        rows = self.rows('observation', 'dd-003')
        policies = self.sql("SELECT o.* FROM seal.observation o JOIN seal.resource r ON r.id=o.resource_id WHERE r.role='policy'")
        self.check('policy fetches create no fabricated SDK result or independent text object',
                   self.origin.count('/robots.txt') >= 1 and bool(policies)
                   and all(o['result_ref'] is None and o['archive_ref'] is None for o in policies))
        self.check('robots denied target is never fetched and never becomes successful Version',
                   self.origin.count('/denied') == 0 and not self.rows('version', 'dd-003'))
        self.check('policy and denial errors remain visible in Observation or Run',
                   any(o['error'] for o in rows) or any(r['status'] == 'failed' for r in self.rows('run')))
        path = self.config('dd-011', ['/network'])
        self.cli('collect', path, '--no-progress', expected=1)
        rows = self.rows('observation', 'dd-011')
        self.check('network failure cannot advance success Version and exposes real error',
                   not self.rows('version', 'dd-011') and bool(rows) and all(r['error'] for r in rows))

    def interrupted_recovery(self):
        path = self.config('dd-004', ['/recovery'], depth=1)
        argv = [sys.executable, '-m', 'seal.cli', 'collect', str(path), '--no-progress']
        process = subprocess.Popen(argv, cwd=ROOT, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            self.origin.wait('/slow'); time.sleep(.4)
            committed = self.rows('observation', 'dd-004')
            self.check('real results commit before interrupted in-flight sibling', bool(committed))
            self.check('no Observation/result fabricated before in-flight resource delivery',
                       not any(o['url'].endswith('/slow') for o in committed))
            os.killpg(process.pid, signal.SIGINT)
            stdout, stderr = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL); process.communicate()
        write(self.artifacts / 'interrupt-receipt.json', dict(exit=process.returncode,
            stdout=self.clean(stdout), stderr=self.clean(stderr)))
        self.commands.append(dict(argv=argv, exit=process.returncode, receipt='interrupt-receipt.json'))
        try:
            receipt = json.loads(stdout)
        except ValueError:
            receipt = {}
        self.check('pre-result Ctrl+C emits real retryable receipt and130',
                   process.returncode == 130 and receipt.get('status') == 'retryable')
        interrupted = self.rows('observation', 'dd-004')
        self.check('interrupt preserves committed results and does not create slow success',
                   all(r in interrupted for r in committed)
                   and not any(r['url'].endswith('/slow') for r in interrupted))
        self.origin.responses['/slow']['delay'] = 0
        run_id = committed[0]['run_id']
        self.cli('worker', '--once')
        recovered = self.rows('observation', 'dd-004')
        self.check('checkpoint recovery retains committed observations and delivers pending sibling once',
                   all(r in recovered for r in committed)
                   and len([r for r in recovered if r['url'].endswith('/slow') and r['run_id'] == run_id]) == 1)
        self.check('public worker resumes original interrupted Run rather than starting new collect',
                   self.sql('SELECT status FROM seal.run WHERE id=%s', (run_id,))[0]['status'] == 'complete'
                   and all(r['run_id'] == run_id for r in recovered))
        duplicates = self.sql('SELECT run_id,url,count(*) FROM seal.observation GROUP BY run_id,url HAVING count(*)>1')
        self.check('real checkpoint delivery is idempotent per Run/resource', not duplicates)
        write(self.artifacts / 'recovery-observations.json', recovered)

    def dom(self):
        self.origin.set('/dom-only', ('<html><head><title>DOM</title></head><body><main>'
            '<h1>DOM contract</h1><p id="target">Placeholder</p></main><script>'
            'document.querySelector("#target").textContent="Rendered Alpha";'
            '</script></body></html>').encode())
        path = self.config('dd-005', ['/dom-only'], mode='rendered_dom', wait_until='load')
        self.cli('collect', path, '--no-progress')
        rows = self.rows('observation', 'dd-005')
        self.check('real browser DOM becomes successful rendered_dom Observation without archive',
                   len(rows) == 1 and rows[0]['input_mode'] == 'rendered_dom'
                   and rows[0]['archive_ref'] is None and bool(rows[0]['version_id']))
        if rows:
            envelope = json.loads(self.body(rows[0]['result_ref']))
            rendered = envelope['result']['html']
            self.check('SDK result contains actual changed DOM and comparison fingerprint',
                       '<p id="target">Rendered Alpha</p>' in rendered
                       and digest(rendered.encode()) == rows[0]['body_hash']
                       and rows[0]['body_hash'] not in self.objects())
        self.cli('recheck', path, '--no-progress')
        self.check('unchanged rendered DOM does not append Version', len(self.rows('version', 'dd-005')) == 1)

    def interrupted_attachment(self):
        from issue20_fixture import long_pdf
        original = long_pdf(); key = digest(original)
        self.origin.set('/retained.pdf', original, 'application/pdf')
        path = self.config('dd-009', ['/retained.pdf'])
        argv = [sys.executable, '-m', 'seal.cli', 'collect', str(path), '--no-progress']
        process = subprocess.Popen(argv, cwd=ROOT, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        checkpoint = None
        try:
            self.origin.wait('/retained.pdf')
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                checkpoints = self.sql('SELECT f.checkpoint FROM seal.frontier f WHERE f.source_id=%s', ('dd-009',))
                checkpoint = checkpoints[0]['checkpoint'] if checkpoints else None
                if key in self.objects() and key in json.dumps(checkpoint):
                    os.killpg(process.pid, signal.SIGSTOP); break
                if process.poll() is not None:
                    break
                time.sleep(.01)
            before = self.rows('observation', 'dd-009')
            self.check('real original attachment archived and checkpointed before any SDK delivery',
                       key in self.objects() and key in json.dumps(checkpoint) and not before)
            write(self.artifacts / 'attachment-before-delivery-checkpoint.json', checkpoint)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=30)
        self.commands.append(dict(argv=argv, exit=process.returncode, receipt='attachment-interrupt-receipt.json'))
        write(self.artifacts / 'attachment-interrupt-receipt.json', dict(exit=process.returncode,
            stdout=self.clean(stdout), stderr=self.clean(stderr)))
        replacement = html('Recovery replacement', 'HTML Beta after original PDF attachment was obtained.')
        self.origin.set('/retained.pdf', replacement, 'text/html')
        original_run = self.sql('SELECT run_id FROM seal.frontier WHERE source_id=%s', ('dd-009',))[0]['run_id']
        self.cli('worker', '--once')
        rows = self.rows('observation', 'dd-009')
        current = rows[-1]
        self.check('attachment recovery resumes exact original Run id', current['run_id'] == original_run)
        envelope = json.loads(self.body(current['result_ref']))
        self.check('changed input recovery retains prior original attachment in result attachment_refs',
                   key in envelope.get('attachment_refs', []) and key in current['attachment_refs']
                   and self.body(key) == original)
        self.check('recovered actual HTML SDK result is not forged into PDF or downloaded_files',
                   current['archive_ref'] is None and current['body_hash'] == digest(replacement)
                   and bool(current['version_id']) and 'HTML Beta' in envelope['result']['html']
                   and not envelope['result'].get('downloaded_files'))
        code = '''import sys,json
from seal.objects import Objects
from seal.archive import deserialize_result
from seal.core import SealError
try:
 deserialize_result(Objects().json(sys.argv[1]));print(json.dumps({'unexpected':'success'}));raise SystemExit(0)
except SealError as e:
 print(json.dumps({'error':e.code}));raise SystemExit(2)
'''
        for fault in ('missing', 'corrupt'):
            if fault == 'missing':
                self.s3.delete_object(Bucket=self.bucket, Key=key)
            else:
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=b'attachment_ref corrupt')
            try:
                _, receipt = self.process(['-c', code, current['result_ref']], expected=2)
                self.check('result reader verifies ' + fault + ' prior-attempt attachment_ref', bool(receipt.get('error')))
            finally:
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=original)
        self.check('changed-input recovery does not duplicate per-Run delivery', not self.sql(
            'SELECT run_id,url,count(*) FROM seal.observation GROUP BY run_id,url HAVING count(*)>1'))

    def executor_exception(self):
        from issue20_fixture import long_pdf
        original = long_pdf(1700); key = digest(original)
        self.origin.set('/executor-exception.pdf', original, 'application/pdf')
        path = self.config('dd-012', ['/executor-exception.pdf'])
        argv = [sys.executable, '-m', 'seal.cli', 'collect', str(path), '--no-progress']
        process = subprocess.Popen(argv, cwd=ROOT, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        checkpoint = None
        try:
            self.origin.wait('/executor-exception.pdf')
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                rows = self.sql('SELECT checkpoint FROM seal.frontier WHERE source_id=%s', ('dd-012',))
                checkpoint = rows[0]['checkpoint'] if rows else None
                if key in self.objects() and key in json.dumps(checkpoint):
                    break
                if process.poll() is not None:
                    break
                time.sleep(.01)
            self.check('actual attachment acquired before executor exception without SDK result delivery',
                       key in self.objects() and key in json.dumps(checkpoint)
                       and not self.rows('observation', 'dd-012'))
            killed = []
            deadline = time.monotonic() + 35
            while process.poll() is None and time.monotonic() < deadline:
                children = subprocess.run(['pgrep', '-P', str(process.pid)], text=True,
                                          capture_output=True).stdout.split()
                for child in children:
                    try:
                        child_pid = int(child)
                        group = os.getpgid(child_pid)
                        if group != os.getpgid(process.pid):
                            os.killpg(group, signal.SIGKILL)
                        else:
                            os.kill(child_pid, signal.SIGKILL)
                        killed.append(child_pid)
                    except ProcessLookupError:
                        pass
                time.sleep(.05)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL); process.communicate()
        rows = self.rows('observation', 'dd-012')
        checkpoints = self.sql('SELECT checkpoint FROM seal.frontier WHERE source_id=%s', ('dd-012',))
        retained = any(key in o['attachment_refs'] for o in rows) or key in json.dumps(checkpoints)
        self.check('pre-result executor failure exposes real error without fabricated result or Version',
                   process.returncode != 0 and not self.rows('version', 'dd-012')
                   and bool(killed) and bool(stdout.strip())
                   and all(o['result_ref'] is None and o['error'] for o in rows))
        self.check('pre-result executor exception retains acquired attachment via SQL or checkpoint',
                   retained and self.body(key) == original)
        write(self.artifacts / 'executor-exception-receipt.json', dict(exit=process.returncode,
            stdout=self.clean(stdout), stderr=self.clean(stderr), observations=rows, checkpoint=checkpoints))
        self.commands.append(dict(argv=argv, exit=process.returncode, receipt='executor-exception-receipt.json'))

    def downstream_inventory(self):
        script = '''import os,json,hashlib
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from seal.objects import Objects
from seal.archive import deserialize_result
with psycopg.connect(os.environ['SEAL_DATABASE_URL'],row_factory=dict_row) as c:
 rows=c.execute('SELECT * FROM seal.observation ORDER BY observed_at,id').fetchall()
out=[]
for o in rows:
 if not o['result_ref']: continue
 envelope=Objects().json(o['result_ref']); r=deserialize_result(envelope)
 files=[{'sha256':hashlib.sha256(Path(p).read_bytes()).hexdigest(),'bytes':len(Path(p).read_bytes())} for p in r.downloaded_files or []]
 out.append({'id':str(o['id']),'url':o['url'],'outcome':o['outcome'],'result_ref':o['result_ref'],
  'success':r.success,'html':r.html,'markdown':str(r.markdown),'files':files,
  'native_fields':list(r.model_fields),'html_match':r.html==envelope['result'].get('html'),
  'headers':r.response_headers,'status':r.status_code,'pdf':bool(r.pdf),'screenshot':bool(r.screenshot),'mhtml':bool(r.mhtml)})
print(json.dumps({'results':out}))'''
        _, rebuilt = self.process(['-c', script])
        results = rebuilt.get('results', []); obs = self.rows('observation')
        self.check('every actual returned result is reconstructable in a new consumer process',
                   len(results) == sum(bool(o['result_ref']) for o in obs)
                   and bool(results) and all(r['html_match'] for r in results))
        self.check('text PDF parses original Alpha rather than hypothetical second Beta input',
                   any(r['url'].endswith('/source.pdf') and r['success']
                       and 'Issue 20 same input PDF Alpha' in r['markdown']
                       and 'forbidden second request Beta' not in r['markdown'] for r in results))
        self.check('actual SDK failure results remain failed after process boundary',
                   any(r['url'].endswith('/article') and not r['success'] for r in results)
                   and any(r['url'].endswith('/unsupported.xlsx') and not r['success'] for r in results))
        self.check('SDK response headers are sanitized in stored complete result',
                   all('synthetic_cookie=issue20_marker' not in json.dumps(r['headers']) for r in results))
        self.check('native result has full SDK fields with no generated page binary products',
                   all(len(r['native_fields']) >= 20 and not r['pdf'] and not r['screenshot']
                       and not r['mhtml'] for r in results))
        write(self.artifacts / 'cross-process-results.json', rebuilt)
        refs = {o['result_ref'] for o in obs if o['result_ref']}
        attachments = {o['archive_ref'] for o in obs if o['archive_ref']}
        attachments.update(ref for o in obs for ref in o['attachment_refs'])
        def file_refs(value):
            if isinstance(value, dict):
                if value.get('archive_ref'):
                    attachments.add(value['archive_ref'])
                attachments.update(value.get('attachment_refs') or [])
                for child in value.values():
                    file_refs(child)
            elif isinstance(value, list):
                for child in value:
                    file_refs(child)
        for run in self.rows('run'):
            file_refs(run['report'].get('undelivered_inputs'))
        inventory = []
        for key in self.objects():
            raw = self.body(key); kind = 'unclassified'
            try:
                envelope = json.loads(raw)
                if isinstance(envelope, dict) and isinstance(envelope.get('result'), dict):
                    kind = 'CrawlResult'
                    attachments.update(envelope['result'].get('downloaded_files') or [])
                    attachments.update(envelope.get('attachment_refs') or [])
                    (self.artifacts / ('result-' + key + '.json')).write_bytes(raw)
            except (ValueError, UnicodeDecodeError):
                pass
            inventory.append(dict(key=key, sha256=digest(raw), bytes=len(raw), category=kind))
        expected_files = set(self.origin.original_files)
        for item in inventory:
            if item['category'] != 'CrawlResult' and item['key'] in expected_files:
                item['category'] = 'original_attachment'
        self.check('actual bucket new writes contain only complete result JSON and original files',
                   bool(inventory) and all(i['category'] in ('CrawlResult', 'original_attachment') for i in inventory),
                   categories={k: sum(i['category'] == k for i in inventory) for k in ('CrawlResult', 'original_attachment', 'unclassified')})
        self.check('all flat CAS keys and downloaded bytes verify SHA256',
                   all(re.fullmatch('[a-f0-9]{64}', i['key']) and i['key'] == i['sha256'] for i in inventory))
        keys = set(self.objects())
        self.check('every SQL result/archive reference is readable and correctly classified',
                   refs <= keys and attachments <= keys
                   and all(next(i['category'] for i in inventory if i['key'] == ref) == 'CrawlResult' for ref in refs))
        self.check('no complete SDK result duplicated in relationship database',
                   not self.sql("SELECT * FROM information_schema.columns WHERE table_schema='seal' AND column_name='crawl_result'")
                   and not self.sql("SELECT id FROM seal.run WHERE report ? 'crawl_results'"))
        self.check('CLI and stored Run reports have no MinIO manifest reference',
                   all('manifest_ref' not in json.dumps(r, default=str) and 'manifest_hash' not in json.dumps(r, default=str)
                       for r in self.rows('run')) and all('manifest_ref' not in p.read_text()
                       and 'manifest_hash' not in p.read_text() for p in self.artifacts.glob('*-receipt.json')))
        write(self.artifacts / 'objects.json', inventory); write(self.artifacts / 'sql.json', self.snapshot())
        before, objects = self.snapshot(), self.objects()
        self.cli('init')
        self.check('repeated initialized schema leaves actual collected rows and objects unchanged',
                   before == self.snapshot() and objects == self.objects())

    def execute(self):
        for method in (self.initialization, self.timeline, self.mixed_resources, self.policy_network,
                       self.interrupted_recovery, self.dom, self.interrupted_attachment, self.executor_exception):
            try:
                method()
            except Exception as exc:
                self.check(method.__name__ + ' completes', False, category=type(exc).__name__, detail=self.clean(str(exc)))
        from issue20_boundaries import boundaries
        boundaries(self)
        from issue20_faults import faults
        try:
            faults(self)
        except Exception as exc:
            self.check('fault scenarios complete', False, category=type(exc).__name__, detail=self.clean(str(exc)))
        self.downstream_inventory()


if __name__ == '__main__':
    lab = Acceptance()
    try:
        lab.start(); lab.execute()
    except Exception as exc:
        lab.check('Issue20 acceptance completed', False, category=type(exc).__name__, detail=lab.clean(str(exc)))
    finally:
        if (lab.artifacts / 'environment.json').exists():
            started = json.loads((lab.artifacts / 'environment.json').read_text())['source_sha256']
            lab.check('complete acceptance started and ended with identical product source bytes',
                      all((ROOT / path).exists() and digest((ROOT / path).read_bytes()) == sha
                          for path, sha in started.items()))
        lab.close()
    raise SystemExit(int(any(c['status'] == 'FAIL' for c in lab.checks)))
