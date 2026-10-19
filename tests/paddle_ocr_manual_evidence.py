#!/usr/bin/env python3
"""Independent provenance/schema/privacy checks, separate from frontier arithmetic."""
import argparse
import hashlib
import json
import re
from pathlib import Path

base = Path('experiments/paddle-ocr-pareto')
parser = argparse.ArgumentParser()
parser.add_argument('--source-snapshot', type=Path)
args = parser.parse_args()
if args.source_snapshot is not None and not args.source_snapshot.is_dir():
    parser.error('--source-snapshot must be an existing frozen source directory')
run = base / 'artifacts/20261019'
manifest = json.loads((run / 'manifest.json').read_text())
checks = []
for category in ('input_files', 'model_files', 'source_files'):
    source_base = args.source_snapshot if category == 'source_files' and args.source_snapshot else base
    failures = [p for p, h in manifest[category].items() if hashlib.sha256((source_base / p).read_bytes()).hexdigest() != h]
    checks.append({'check': category + ' exact SHA256', 'passed': not failures, 'count': len(manifest[category]), 'failures': failures})
checks.append({'check': 'single-thread runtime', 'passed': manifest['runtime']['opencv_threads'] == 1 and all(manifest['runtime']['environment'].get(k) == '1' for k in ('OMP_NUM_THREADS', 'OMP_THREAD_LIMIT', 'OPENBLAS_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')), 'evidence': manifest['runtime']})
converted = lines = 0
failures = []
for raw_path in run.glob('PP-OCR*/**/raw.json'):
    raw = json.loads(raw_path.read_text())['res']
    words = json.loads(raw_path.with_name('words.json').read_text())
    expected = []
    for text, fragments, boxes in zip(raw['rec_texts'], raw['text_word'], raw['text_word_boxes'], strict=True):
        lines += 1
        if ''.join(fragments) != text:
            failures.append({'path': str(raw_path), 'reason': 'fragments differ from rec_text'})
        for i, (fragment, box) in enumerate(zip(fragments, boxes, strict=True)):
            expected.append({'text': fragment, 'bbox': box, 'separator': ' ' if i == 0 else ''})
    if words != expected:
        failures.append({'path': str(raw_path), 'reason': 'word transformation differs'})
    if raw_path.with_name('text.txt').read_text().rstrip('\n') != '\n'.join(raw['rec_texts']).rstrip('\n'):
        failures.append({'path': str(raw_path), 'reason': 'text.txt differs from rec_text'})
    converted += 1
checks.append({'check': 'Paddle raw fragments/boxes -> words preserves characters/order/coordinates', 'passed': not failures, 'pages_repetitions': converted, 'lines': lines, 'failures': failures})
startups = []
for path in run.glob('PP-OCR*/*startup*.json'):
    startups.append({'path': str(path), 'data': json.loads(path.read_text())})
checks.append({'check': 'cold startup separated', 'passed': len(startups) >= 3, 'evidence': startups, 'boundary': manifest['timing_contract']})
patterns = [r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', r'\b(?:sk|ghp|hf)_[A-Za-z0-9]{24,}\b', r'(?i)(?:authorization["\s]*[:=]["\s]*bearer\s+)[A-Za-z0-9._-]{16,}', r'(?i)(?:api_key|secret_key|password)["\s]*[:=]["\s]*[A-Za-z0-9_/-]{16,}']
secret_hits = []
scanned = 0
for path in run.rglob('*'):
    if path.is_file() and path.suffix in {'.json', '.log', '.txt', '.md', '.csv', '.tsv'}:
        text = path.read_text(errors='replace')
        scanned += 1
        for index, pattern in enumerate(patterns):
            if re.search(pattern, text):
                secret_hits.append({'path': str(path), 'pattern_index': index})
checks.append({'check': 'artifact credential-pattern scan', 'passed': not secret_hits, 'files_scanned': scanned, 'hits_without_values': secret_hits, 'limit': 'Pattern scan plus public-input provenance; not a guarantee against arbitrary secrets. Public archival document headers can contain public contact data.'})
calibration = base / 'work/20261019/calibration-before-opencv-thread-limit'
checks.append({'check': 'superseded calibration isolated from formal artifact tree', 'passed': calibration.is_dir() and not (run / 'calibration-before-opencv-thread-limit').exists(), 'path': str(calibration)})
visual = [
 {'language':'vie','regions':12,'verdict':'pass','evidence':'artifacts/paddle-ocr-independent-20261019/vietnamese-truth-visual-review.json'},
 {'language':'chi_sim','page':'public-scan-b-p2','regions':3,'verdict':'pass','image':'artifacts/tesseract-multilang-20261016/chi_sim/public-scan-b-page-2-300dpi.png','finding':'原图三处固定段落与 gold 一致；tiny120 零 CER 页面对应输入，无更换真值迹象。'},
 {'language':'eng','page':'nara-recommendation-2018-p1','region':'middle','verdict':'pass','image':'artifacts/tesseract-multilang-20261016/eng/input/nara-recommendation-2018-p1.png','finding':'原图以 The 开头，没有左引号；tessdata_fast96 输出前置左引号为真实 OCR 差异，不是真值漏字。'}]
report = {'passed':all(c['passed'] for c in checks), 'checks':checks, 'visual_reviews':visual,
 'source_snapshot':str(args.source_snapshot) if args.source_snapshot else None,
 'source_scope':'Explicit frozen historical source audit; does not establish current source produced historical timings' if args.source_snapshot else 'Current experiment source hashes must match historical manifest',
 'upstream':{'checked':'2026-10-19','latest_release':'3.7.0','latest_release_url':'https://github.com/PaddlePaddle/PaddleOCR/releases/latest','resolved_release_url':'https://github.com/PaddlePaddle/PaddleOCR/releases/tag/v3.7.0','model_repositories':['https://huggingface.co/PaddlePaddle/PP-OCRv6_'+x+'_rec' for x in ('tiny','small','medium')],'finding':'官方 latest 指向 3.7.0，发布 PP-OCRv6 tiny/small/medium；三档识别模型官方仓库页面可访问。模型内容逐文件 hash 已核，但未重新下载第二份远端模型字节比对。'},
 'limitations':['本回执由最终815次页面运行完成后重新执行生成；未来工件变更需重新验收','源码仅核 hash，未读取实验实现设计验收；计时边界依据 runtime、startup 与逐次回执声明，未系统级跟踪 CPU 实际工作线程','当前只核部分典型原图及全部越南语区域；不能宣称所有语言全部真值独立复核']}
out=Path('artifacts/paddle-ocr-independent-20261019/manual-review.json');out.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'passed':report['passed'],'checks':[(c['check'],c['passed']) for c in checks],'conversion_failure_count':len(failures)},ensure_ascii=False))
raise SystemExit(0 if report['passed'] else 1)
