"""Real new-Run evidence handoff and multiple original files from different attempts."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

from issue20_fixture import html, long_pdf
from issue20_lab import ROOT, digest, write


def acquired_then_killed(lab, ident, endpoint, pages):
    original = long_pdf(pages); key = digest(original)
    lab.origin.set(endpoint, original, 'application/pdf')
    path = lab.config(ident, [endpoint])
    argv = [sys.executable, '-m', 'seal.cli', 'collect', str(path), '--no-progress']
    process = subprocess.Popen(argv, cwd=ROOT, env=lab.env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    frontier = None
    try:
        lab.origin.wait(endpoint)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            frontiers = lab.sql('SELECT * FROM seal.frontier WHERE source_id=%s', (ident,))
            frontier = frontiers[0] if frontiers else None
            if key in lab.objects() and key in json.dumps(frontier, default=str):
                os.killpg(process.pid, signal.SIGSTOP); break
            if process.poll() is not None:
                break
            time.sleep(.01)
        lab.check(ident + ' captures real archive/checkpoint before SDK result delivery',
                  frontier is not None and key in lab.objects()
                  and key in json.dumps(frontier['checkpoint']) and not lab.rows('observation', ident))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=30)
    receipt = ident + '-pre-result-interrupt.json'
    write(lab.artifacts / receipt, dict(exit=process.returncode, stdout=lab.clean(stdout),
          stderr=lab.clean(stderr), frontier=frontier, acquired_file_sha256=key))
    lab.commands.append(dict(argv=argv, exit=process.returncode, receipt=receipt))
    return path, original, key, frontier


def new_run(lab):
    path, original, key, frontier = acquired_then_killed(lab, 'dd-013', '/new-run.pdf', 1600)
    old_run = frontier['run_id']
    old_report = lab.sql('SELECT report FROM seal.run WHERE id=%s', (old_run,))[0]['report']
    replacement = html('New Run Beta', 'A new Run uses its own actual HTML input.')
    lab.origin.set('/new-run.pdf', replacement)
    for fault in ('missing', 'corrupt'):
        if fault == 'missing':
            lab.s3.delete_object(Bucket=lab.bucket, Key=key)
        else:
            lab.s3.put_object(Bucket=lab.bucket, Key=key, Body=b'new-run handoff corrupt file')
        try:
            lab.cli('collect', path, '--no-progress', expected=(1, 2))
            actual = lab.sql('SELECT * FROM seal.frontier WHERE source_id=%s', ('dd-013',))[0]
            lab.check('new Run handoff verifies ' + fault + ' prior file before replacing checkpoint',
                      actual['run_id'] == old_run and actual['checkpoint'] == frontier['checkpoint']
                      and not lab.rows('version', 'dd-013')
                      and lab.sql('SELECT report FROM seal.run WHERE id=%s', (old_run,))[0]['report'] == old_report)
        finally:
            lab.s3.put_object(Bucket=lab.bucket, Key=key, Body=original)
    lab.cli('collect', path, '--no-progress')
    old = lab.sql('SELECT * FROM seal.run WHERE id=%s', (old_run,))[0]
    current = lab.rows('observation', 'dd-013')[-1]
    envelope = json.loads(lab.body(current['result_ref']))
    lab.check('new Run handoff retains undelivered original file in immutable old Run evidence',
              key in json.dumps(old['report'].get('undelivered_inputs'))
              and lab.body(key) == original and current['run_id'] != old_run)
    lab.check('old undelivered input does not fabricate old Observation or contaminate new SDK result',
              all(o['run_id'] != old_run for o in lab.rows('observation', 'dd-013'))
              and current['archive_ref'] is None and current['body_hash'] == digest(replacement)
              and key not in current['attachment_refs'] and key not in envelope.get('attachment_refs', [])
              and 'A new Run uses its own actual HTML input.' in envelope['result']['html'])
    write(lab.artifacts / 'new-run-evidence-handoff.json', dict(old_run=old,
          current_observation=current, current_result=envelope))
    # Earlier sustained executor failures never yielded any SDK result, but acquired a file.
    failure_frontier = lab.sql('SELECT * FROM seal.frontier WHERE source_id=%s', ('dd-012',))[0]
    failure_run = failure_frontier['run_id']
    acquired = failure_frontier['checkpoint']['acquired']
    file_key = next(value['archive_ref'] for value in acquired.values())
    lab.origin.set('/executor-exception.pdf', html('After exception', 'New actual input after no-result executor failure.'))
    path = lab.work / 'dd-012.yaml'
    lab.cli('collect', path, '--no-progress')
    previous = lab.sql('SELECT * FROM seal.run WHERE id=%s', (failure_run,))[0]
    lab.check('no-SDK-result executor failure remains auditable after next Run clears checkpoint',
              file_key in json.dumps(previous['report'].get('undelivered_inputs'))
              and digest(lab.body(file_key)) == file_key
              and not any(o['run_id'] == failure_run for o in lab.rows('observation', 'dd-012')))
    write(lab.artifacts / 'no-result-new-run-evidence.json', previous)


def multiple_files(lab):
    path, original, first, frontier = acquired_then_killed(lab, 'dd-014', '/multiple.xlsx', 1500)
    second_body = b'Issue20 second actual unsupported attachment after first PDF acquisition'
    second = digest(second_body)
    lab.origin.set('/multiple.xlsx', second_body,
                   'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    lab.cli('worker', '--once')
    current = lab.rows('observation', 'dd-014')[-1]
    envelope = json.loads(lab.body(current['result_ref']))
    lab.check('real SDK failure after two acquired files keeps each original attachment explicit',
              current['run_id'] == frontier['run_id'] and current['archive_ref'] == second
              and {first, second} <= set(current['attachment_refs'])
              and {first, second} <= set(envelope.get('attachment_refs', []))
              and not current['version_id'] and not envelope['result']['success'])
    code = '''import sys,json,hashlib
from seal.objects import Objects
from seal.archive import deserialize_result
envelope=Objects().json(sys.argv[1]);r=deserialize_result(envelope)
files=[{'key':key,'sha256':hashlib.sha256(Objects().get(key)).hexdigest(),'bytes':len(Objects().get(key))} for key in envelope['attachment_refs']]
print(json.dumps({'files':files,'success':r.success}))'''
    _, rebuilt = lab.process(['-c', code, current['result_ref']])
    lab.check('each original file of one real result is unambiguously recoverable cross process',
              {r['key'] for r in rebuilt.get('files', [])} >= {first, second}
              and all(r['key'] == r['sha256'] for r in rebuilt.get('files', []))
              and lab.body(first) == original and lab.body(second) == second_body)
    write(lab.artifacts / 'multiple-real-attachments.json', dict(observation=current,
          result=envelope, consumer=rebuilt))


def orphan_recheck(lab):
    path = lab.work / 'dd-002.yaml'
    previous = lab.sql('SELECT run_id FROM seal.frontier WHERE source_id=%s', ('dd-002',))[0]['run_id']
    before = lab.origin.count('/source.pdf')
    lab.origin.set('/index', html('Index without navigation', 'Previously discovered resources disappear from links.'))
    lab.cli('recheck', path, '--no-progress', expected=1)
    observations = lab.rows('observation', 'dd-002')
    lab.check('known resource is rechecked after disappearing from entry navigation',
              lab.origin.count('/source.pdf') == before + 1
              and len([o for o in observations if o['url'].endswith('/source.pdf')]) == 2)
    old = lab.sql('SELECT report FROM seal.run WHERE id=%s', (previous,))[0]['report']
    lab.check('new Run evidence handoff excludes inputs already delivered as Observations',
              not old.get('undelivered_inputs'))
    write(lab.artifacts / 'orphan-resource-recheck.json', dict(previous_run_report=old,
          observations=observations))


def boundaries(lab):
    for method in (new_run, multiple_files, orphan_recheck):
        try:
            method(lab)
        except Exception as exc:
            lab.check(method.__name__ + ' completes', False, category=type(exc).__name__, detail=lab.clean(str(exc)))
