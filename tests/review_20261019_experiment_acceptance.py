#!/usr/bin/env python3
"""Independent public-input CLI acceptance. Does not change experiment history."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'experiments/paddle-ocr-pareto'
WORK = ROOT / 'work/review-20261019-experiment'
OUT = ROOT / 'artifacts/review-20261019-experiment'
PYTHON = SOURCE / '.venv/bin/python'
RESULTS = []


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def record(name, condition, details):
    RESULTS.append({'case': name, 'passed': bool(condition), 'details': details})


def clone(name):
    target = WORK / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for file in SOURCE.glob('*.py'):
        shutil.copyfile(file, target / file.name)
    for file in ['pyproject.toml', 'uv.lock']:
        shutil.copyfile(SOURCE / file, target / file)
    shutil.copytree(SOURCE / 'config', target / 'config')
    settings = read(target / 'config/benchmark.json')
    settings.update(run='20261019', models=['tessdata_fast'], dpis=[96, 120], repeats=3)
    save(target / 'config/benchmark.json', settings)
    refs = read(target / 'config/references.json')
    for item in refs.values():
        item['pages'] = item['pages'][:1]
    save(target / 'config/references.json', refs)
    policy = read(target / 'config/screening.json')
    policy.update(coarse_dpis=[96], refinement_neighbors={})
    save(target / 'config/screening.json', policy)
    for path in read(target / 'config/documents.json'):
        dest = target / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / path, dest)
    shutil.copytree(SOURCE / 'work/20261019/models', target / 'work/20261019/models')
    return target


def cli(target, label, *args, env=None):
    command = [str(PYTHON), str(target / 'run.py'), *args]
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
    (OUT / f'{label}.stdout.txt').write_text(result.stdout)
    (OUT / f'{label}.stderr.txt').write_text(result.stderr)
    save(OUT / f'{label}.command.json', {'command': command, 'cwd': str(ROOT), 'exit_code': result.returncode})
    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    base = clone('fresh-resume')
    first = cli(base, 'fresh')
    data = base / 'artifacts/20261019'
    summary = read(data / 'summary.json')
    receipts = list(data.glob('tessdata_fast/*/96/*/*/receipt.json'))
    record('fresh real four-language one-page CLI', first.returncode == 0 and len(receipts) == 12
           and all(read(p)['status'] == 'ok' for p in receipts)
           and all(x['stage'] == 'replicated' for x in summary['configurations'] if x['dpi'] == 96),
           {'exit_code': first.returncode, 'receipts': len(receipts), 'scope': '4 languages x 1 real page x 3 repetitions, 96 DPI only'})
    save(OUT / 'fresh-summary.json', summary)
    # Resume from a missing final receipt using a different engine executable.
    engine = Path(shutil.which('tesseract')).resolve()
    bindir = WORK / 'runtime-bin'
    bindir.mkdir(exist_ok=True)
    wrapper = bindir / 'tesseract'
    wrapper.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then\n echo "tesseract acceptance runtime changed"\n exit 0\nfi\nexec ' + str(engine) + ' "$@"\n')
    wrapper.chmod(0o755)
    missing = next(p for p in receipts if '/eng/' in str(p) and p.parent.name == '2')
    before = read(data / 'manifest.json')['runtime']
    missing.unlink()
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
    resumed = cli(base, 'runtime-resume', env=env)
    after = read(data / 'manifest.json')['runtime']
    actual_hash = hashlib.sha256(wrapper.read_bytes()).hexdigest()
    record('resume rejects changed OCR executable', resumed.returncode != 0,
           {'exit_code': resumed.returncode, 'missing_receipt_regenerated': missing.exists(),
            'runtime_manifest_preserved': before == after, 'frozen_binary_sha256': after['tesseract_binary_sha256'],
            'actual_binary_sha256': actual_hash, 'actual_version': subprocess.check_output([str(wrapper), '--version'], text=True).strip()})
    recovered = cli(base, 'same-runtime-resume')
    record('same-runtime interrupted CLI resumes', recovered.returncode == 0 and missing.exists(),
           {'exit_code': recovered.returncode, 'missing_receipt_regenerated': missing.exists()})
    complete_report = cli(base, 'complete-report', '--report-only')
    record('offline report accepts complete confirmed run', complete_report.returncode == 0,
           {'exit_code': complete_report.returncode})
    completed = WORK / 'completed-artifacts'
    if completed.exists():
        shutil.rmtree(completed)
    shutil.copytree(data, completed)
    # An interruption after coarse screening leaves only repeat zero.
    for path in list(data.glob('tessdata_fast/*/96/*/*/receipt.json')):
        if path.parent.name in {'1', '2'}:
            shutil.rmtree(path.parent)
    offline = cli(base, 'coarse-report', '--report-only')
    coarse = read(data / 'summary.json')
    coarse_frontier = [p for p in coarse['configurations'] if p['pareto']]
    record('offline report rejects unconfirmed frontier', offline.returncode != 0,
           {'exit_code': offline.returncode, 'published_frontier': [{'id': p['id'], 'stage': p['stage'], 'minimum_repeats': p['minimum_repeats']} for p in coarse_frontier],
            'pareto_json_published': (data / 'pareto.json').exists()})
    save(OUT / 'coarse-report-summary.json', coarse)
    shutil.copyfile(data / 'report.md', OUT / 'coarse-report.md')
    shutil.rmtree(data)
    shutil.copytree(completed, data)
    # A failed attempted DPI must remain distinguishable from an untested DPI.
    references = read(base / 'config/references.json')
    page = references['eng']['pages'][0]
    folder = data / 'tessdata_fast/eng/120' / f"{page['document_id']}-p{page['page_number']}" / '0'
    save(folder / 'receipt.json', {'status': 'failed', 'error_type': 'TimeoutError', 'error': 'isolated acceptance fault', 'total_seconds': 1.0, 'repetition': 0, 'files': {}})
    failed = cli(base, 'failed-report', '--report-only')
    failed_summary = read(data / 'summary.json')
    row = next(r for r in failed_summary['configurations'] if r['id'] == 'tessdata_fast-eng-120')
    record('failed attempted configuration remains distinct from untested', row['stage'] != 'not_evaluated',
           {'exit_code': failed.returncode, 'configuration': row})
    save(OUT / 'failed-report-summary.json', failed_summary)
    # Observe effective constructor arguments at the public PaddleOCR boundary.
    script = """import sys, types, json, os
sys.path.insert(0, sys.argv[1])
from pathlib import Path
config=Path(sys.argv[1])/'config/benchmark.json'
settings=json.loads(config.read_text())
settings.update(cpu_threads=2,detection_limit_side_len=512,detection_limit_type='min',paddle_word_boxes=False,text_rec_score_thresh=.8)
config.write_text(json.dumps(settings))
os.environ['PADDLE_PDX_CACHE_HOME']='/tmp/acceptance-external-model-cache'
seen={}
class Capture:
 def __init__(self, **kwargs): seen.update(kwargs)
sys.modules['paddleocr']=types.SimpleNamespace(PaddleOCR=Capture)
import prepare
prepare.paddle_model('tiny')
print(json.dumps({'requested': settings, 'effective':seen, 'cache_home':os.environ['PADDLE_PDX_CACHE_HOME']}))
"""
    captured = subprocess.run([str(PYTHON), '-c', script, str(base)], capture_output=True, text=True, check=True)
    args = json.loads(captured.stdout)
    save(OUT / 'paddle-effective-configuration.json', args)
    effective = args['effective']
    record('editable predictor configuration reaches engine', effective['cpu_threads'] == 2
           and effective['text_det_limit_side_len'] == 512 and effective['text_det_limit_type'] == 'min'
           and effective['return_word_box'] is False and effective['text_rec_score_thresh'] == .8,
           {'requested_keys': {k: args['requested'][k] for k in ['cpu_threads','detection_limit_side_len','detection_limit_type','paddle_word_boxes','text_rec_score_thresh']}, 'effective': effective})
    record('prepare forces experiment-local model cache', args['cache_home'] == str(base / 'work/20261019/paddlex-cache'),
           {'effective_cache_home': args['cache_home']})
    absent = clone('missing-model')
    settings = read(absent / 'config/benchmark.json')
    settings['models'] = ['tessdata_fast', 'PP-OCRv6_tiny', 'PP-OCRv6_small', 'PP-OCRv6_medium']
    save(absent / 'config/benchmark.json', settings)
    absent_result = cli(absent, 'missing-model')
    record('fresh run rejects missing required models before freezing', absent_result.returncode != 0
           and 'Missing prepared model' in absent_result.stderr and not (absent / 'artifacts/20261019/manifest.json').exists(),
           {'exit_code': absent_result.returncode, 'manifest_created': (absent / 'artifacts/20261019/manifest.json').exists()})
    cpu = clone('invalid-threads')
    settings = read(cpu / 'config/benchmark.json')
    settings['cpu_threads'] = 2
    save(cpu / 'config/benchmark.json', settings)
    cpu_result = cli(cpu, 'invalid-threads')
    record('fresh run rejects CPU thread-contract violation before freezing', cpu_result.returncode != 0
           and 'cpu_threads=1' in cpu_result.stderr and not (cpu / 'artifacts/20261019/manifest.json').exists(),
           {'exit_code': cpu_result.returncode, 'manifest_created': (cpu / 'artifacts/20261019/manifest.json').exists()})
    tiny = clone('tiny-real-single-page')
    settings = read(tiny / 'config/benchmark.json')
    settings.update(models=['PP-OCRv6_tiny'], languages=['chi_sim'], dpis=[96], repeats=1)
    save(tiny / 'config/benchmark.json', settings)
    for kind in ['det', 'rec']:
        name = 'PP-OCRv6_tiny_' + kind
        shutil.copytree(SOURCE / 'work/20261019/paddlex-cache/official_models' / name,
                        tiny / 'work/20261019/paddlex-cache/official_models' / name)
    command = [str(PYTHON), str(tiny / 'benchmark.py'), '--engine', 'PP-OCRv6_tiny']
    actual = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=240)
    (OUT / 'tiny-real-single-page.stdout.txt').write_text(actual.stdout)
    (OUT / 'tiny-real-single-page.stderr.txt').write_text(actual.stderr)
    save(OUT / 'tiny-real-single-page.command.json', {'command': command, 'cwd': str(ROOT), 'exit_code': actual.returncode})
    tiny_data = tiny / 'artifacts/20261019'
    tiny_summary = read(tiny_data / 'summary.json') if (tiny_data / 'summary.json').exists() else {}
    tiny_receipts = list(tiny_data.glob('PP-OCRv6_tiny/chi_sim/96/*/0/receipt.json'))
    raw = read(tiny_receipts[0].parent / 'raw.json')['res'] if tiny_receipts else {}
    record('current real tiny single-page acquisition and geometry scoring', actual.returncode == 0
           and len(tiny_receipts) == 1 and read(tiny_receipts[0])['status'] == 'ok'
           and len(tiny_summary.get('configurations', [])) == 1 and tiny_summary['configurations'][0]['eligible']
           and raw.get('return_word_box') is True and raw.get('text_rec_score_thresh') == 0.0,
           {'exit_code': actual.returncode, 'receipts': len(tiny_receipts), 'summary': tiny_summary,
            'scope': 'one real Simplified Chinese page, tiny model, 96 DPI, one repetition; no timing-generalization claim'})
    save(OUT / 'tiny-real-single-page-summary.json', tiny_summary)
    save(OUT / 'acceptance.json', {'results': RESULTS, 'environment': {'python': str(PYTHON), 'tesseract': str(engine)},
          'command': 'experiments/paddle-ocr-pareto/.venv/bin/python tests/review_20261019_experiment_acceptance.py',
          'data_prerequisites': 'Existing pinned public PDF and tessdata_fast bytes in experiment work/20261019; locked experiment .venv; real tesseract executable; no DB/network/secrets',
          'limitations': ['Uses one original page per language, not full 16-page matrix', 'Paddle configuration case observes constructor API arguments; no full Paddle matrix rerun']})
    for item in RESULTS:
        print(('PASS ' if item['passed'] else 'FAIL ') + item['case'])
    raise SystemExit(0 if all(item['passed'] for item in RESULTS) else 1)


if __name__ == '__main__':
    main()
