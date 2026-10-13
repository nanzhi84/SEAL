"""Issue20 real-service fault injection; mutations target this run's isolated bucket."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import boto3
from botocore.config import Config

from issue20_fixture import html, pdf
from issue20_lab import ROOT, digest, write


def faults(lab):
    observations = lab.rows('observation')
    result_key = next(o['result_ref'] for o in observations if o['result_ref'])
    file_key = next(o['archive_ref'] for o in observations if o['archive_ref'])
    stable = lab.snapshot()
    for kind, key in (('result', result_key), ('original_file', file_key)):
        saved = lab.body(key)
        code = '''import json,sys
from seal.objects import Objects
from seal.core import SealError
try:
 Objects().get(sys.argv[1]);print(json.dumps({'unexpected':'read succeeded'}));raise SystemExit(0)
except SealError as e:
 print(json.dumps({'error':e.code}));raise SystemExit(2)
'''
        for fault in ('missing', 'corrupt'):
            if fault == 'missing':
                lab.s3.delete_object(Bucket=lab.bucket, Key=key)
            else:
                lab.s3.put_object(Bucket=lab.bucket, Key=key, Body=b'fault injected corruption')
            _, receipt = lab.process(['-c', code, key], expected=2)
            lab.check(f'{fault} {kind} downstream read explicitly detects the fault',
                      bool(receipt.get('error')), error=receipt.get('error'))
            lab.check(f'{fault} {kind} read does not mutate relationship or success timeline',
                      stable == lab.snapshot())
            lab.s3.put_object(Bucket=lab.bucket, Key=key, Body=saved)
        lab.check(kind + ' original bytes restored through S3 API and reverified',
                  lab.body(key) == saved and digest(saved) == key)
    # A corrupt pre-existing content key must fail validation before a DB pointer commits.
    original = pdf('Issue20 corruption before commit target')
    key = digest(original)
    lab.origin.set('/precommit.pdf', original, 'application/pdf')
    path = lab.config('dd-006', ['/precommit.pdf'])
    lab.s3.put_object(Bucket=lab.bucket, Key=key, Body=b'wrong bytes under expected key')
    lab.cli('collect', path, '--no-progress', expected=1)
    lab.check('corrupt original file cannot commit unreadable archive reference or success Version',
              not lab.rows('version', 'dd-006')
              and not any(o['archive_ref'] == key or key in o['attachment_refs']
                          for o in lab.rows('observation', 'dd-006')))
    lab.check('CAS fault cannot silently overwrite corrupt existing attachment',
              lab.body(key) == b'wrong bytes under expected key')
    lab.s3.delete_object(Bucket=lab.bucket, Key=key)
    lab.cli('collect', path, '--no-progress')
    successful = lab.rows('observation', 'dd-006')
    lab.check('repair and retry recovers exact original PDF with successful Version',
              any(o['archive_ref'] == key and o['version_id'] for o in successful)
              and lab.body(key) == original)
    # Real result storage fault, using deterministic real HTTP/SDK input, no invented JSON.
    lab.origin.set('/result-fault', html('Result fault', 'Real deterministic SDK input.'))
    path = lab.config('dd-007', ['/result-fault'])
    lab.cli('collect', path, '--no-progress')
    before = lab.rows('observation', 'dd-007')
    versions = lab.rows('version', 'dd-007')
    key = before[-1]['result_ref']; saved = lab.body(key)
    actual_json = lab.work / 'actual-crawlresult.json'; actual_json.write_bytes(saved)
    put_code = '''import sys,json
from pathlib import Path
from seal.objects import Objects
from seal.core import SealError
try:
 print(json.dumps({'key':Objects().put(Path(sys.argv[1]).read_bytes())}))
except SealError as e:
 print(json.dumps({'error':e.code}));raise SystemExit(2)
'''
    original_objects = set(lab.objects())
    _, reused = lab.process(['-c', put_code, actual_json])
    lab.check('exact real SDK JSON byte replay reuses existing verified CAS object',
              reused.get('key') == key and set(lab.objects()) == original_objects and lab.body(key) == saved)
    lab.s3.put_object(Bucket=lab.bucket, Key=key, Body=b'corrupted full result')
    _, receipt = lab.process(['-c', put_code, actual_json], expected=2)
    lab.check('exact identical JSON write explicitly rejects existing corrupted object',
              bool(receipt.get('error')) and lab.body(key) == b'corrupted full result'
              and lab.rows('observation', 'dd-007') == before
              and lab.rows('version', 'dd-007') == versions)
    lab.s3.put_object(Bucket=lab.bucket, Key=key, Body=saved)
    # Object service becomes unavailable only after the actual HTTP input was requested.
    path = lab.config('dd-008', ['/write-outage'])
    lab.origin.set('/write-outage', html('Write outage', 'New input obtained before S3 loss.'), delay=6)
    argv = [sys.executable, '-m', 'seal.cli', 'collect', str(path), '--no-progress']
    process = subprocess.Popen(argv, cwd=ROOT, env=lab.env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    stopped = False
    try:
        lab.origin.wait('/write-outage')
        minio = next(c for c in lab.containers if c.endswith('-minio'))
        lab.docker('stop', '--time', '1', minio); stopped = True
        stdout, stderr = process.communicate(timeout=60)
        lab.check('mid-run object service outage emits explicit failing receipt',
                  process.returncode != 0 and bool(stdout.strip()), exit=process.returncode)
        lab.check('mid-run object write failure cannot leave committed result reference or Version',
                  not lab.rows('version', 'dd-008')
                  and not any(o['result_ref'] or o['archive_ref'] for o in lab.rows('observation', 'dd-008')))
        write(lab.artifacts / 'object-outage-receipt.json', dict(exit=process.returncode,
              stdout=lab.clean(stdout), stderr=lab.clean(stderr)))
        lab.commands.append(dict(argv=argv, exit=process.returncode, receipt='object-outage-receipt.json'))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL); process.communicate()
        if stopped:
            lab.docker('start', minio)
            port = lab.docker('port', minio, '9000').rsplit(':', 1)[1]
            lab.endpoint = f'http://127.0.0.1:{port}'; lab.env['SEAL_S3_ENDPOINT'] = lab.endpoint
            lab.s3.close()
            lab.s3 = boto3.client('s3', endpoint_url=lab.endpoint,
                aws_access_key_id=lab.secrets[1], aws_secret_access_key=lab.secrets[2],
                region_name='us-east-1', config=Config(retries={'max_attempts': 0},
                    connect_timeout=2, read_timeout=3, s3={'addressing_style': 'path'}))
            deadline = time.monotonic() + 40
            while True:
                try:
                    lab.s3.head_bucket(Bucket=lab.bucket); break
                except Exception:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(.2)
    lab.origin.responses['/write-outage']['delay'] = 0
    lab.cli('collect', path, '--no-progress')
    lab.check('retry after service restoration retains real result and valid success Version',
              any(o['result_ref'] and o['version_id'] for o in lab.rows('observation', 'dd-008')))
    lab.check('all real run/resource deliveries remain unique after storage retries', not lab.sql(
        'SELECT run_id,url,count(*) FROM seal.observation GROUP BY run_id,url HAVING count(*)>1'))
