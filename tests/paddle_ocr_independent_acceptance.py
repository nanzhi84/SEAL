#!/usr/bin/env python3
"""Independently verify public experiment receipts; never import implementation.
Run: python3 tests/paddle_ocr_independent_acceptance.py --experiment experiments/paddle-ocr-pareto
"""
import argparse
import hashlib
import json
import math
import re
import statistics
import unicodedata
from pathlib import Path


def normalize(value, language):
    value = unicodedata.normalize('NFC', value)
    return re.sub(r'\s+', '', value) if language.lower().startswith(('zh', 'chi', 'chinese')) else ' '.join(value.split())


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        nxt = [i]
        for j, y in enumerate(b, 1):
            nxt.append(min(nxt[-1] + 1, row[j] + 1, row[j-1] + (x != y)))
        row = nxt
    return row[-1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--run', default='20261019')
    parser.add_argument('--out', type=Path, default=Path('artifacts/paddle-ocr-independent-20261019/acceptance.json'))
    parser.add_argument('--source-snapshot', type=Path,
                        help='Explicit historical source root for source_files hashes only; never falls back')
    args = parser.parse_args()
    base = args.experiment.resolve()
    source_base = args.source_snapshot.resolve() if args.source_snapshot is not None else base
    if not source_base.is_dir():
        parser.error('--source-snapshot must be an existing source directory')
    run = base / 'artifacts' / args.run
    manifest_path = run / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    screening_path = run / 'screening-manifest.json'
    screening = json.loads(screening_path.read_text()) if screening_path.exists() else None
    records = json.loads((run / 'records.json').read_text())
    summary = json.loads((run / 'summary.json').read_text())
    checks = []
    def check(condition, label):
        checks.append({'assertion': label, 'passed': bool(condition)})
    def same(a, b):
        return isinstance(a, (int, float)) and math.isfinite(a) and math.isclose(a, b, abs_tol=1e-10, rel_tol=1e-10)
    def verify_file(path, digest, *, source=False):
        root = source_base if source else base
        target = (root / path).resolve()
        return target.is_relative_to(root) and target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest
    check(manifest['path_base'] == 'experiment_root', 'manifest 路径基准')
    check(hashlib.sha256(manifest_path.read_bytes()).hexdigest() == summary['manifest_sha256'], 'summary 引用冻结 manifest')
    if screening is not None:
        check(screening['original_manifest_sha256'] == hashlib.sha256(manifest_path.read_bytes()).hexdigest(), '粗筛引用原始manifest')
        check(summary['screening_manifest_sha256'] == hashlib.sha256(screening_path.read_bytes()).hexdigest(), 'summary引用粗筛manifest')
        check(screening['policy'] == json.loads((base / 'config/screening.json').read_text()) == summary['policy'], '粗筛policy冻结一致')
        check(screening['policy']['time_guard_ratio'] == 1.25 and screening['policy']['confirmation_repeats'] == 3, 'guard与重复策略')
        check(screening['policy']['refinement_neighbors'] == {'96':[120], '144':[120,200], '300':[240]}, '邻居DPI策略')
        check(bool(screening['limitations']) and bool(summary['scope']), '粗筛局限与范围披露')
        for path, digest in screening['source_files'].items():
            check(verify_file(path, digest, source=True), '粗筛源码hash: ' + path)
    entry = json.loads((run / 'entry-receipt.json').read_text())
    check(bool(entry['command']) and bool(entry['preparation']), '复跑入口与环境前提')
    for path, digest in entry['source_files'].items():
        check(verify_file(path, digest, source=True), '入口源码hash: ' + path)
    for category in ('input_files', 'model_files', 'source_files'):
        check(bool(manifest[category]), category + ' 非空')
        for path, digest in manifest[category].items():
            check(verify_file(path, digest, source=category == 'source_files'), category + ' hash: ' + path)
    for key in ('config', 'runtime', 'timing_contract', 'scoring_contract', 'limitations'):
        check(bool(manifest.get(key)), 'manifest 声明 ' + key)
    references = json.loads((base / 'config/references.json').read_text())
    gold_regions = {region['id']: region for lang in references.values() for page in lang['pages'] for region in page['regions']}
    check(manifest['runtime'].get('opencv_threads') == 1, 'OpenCV 实际线程为1')
    check(all(manifest['runtime']['environment'].get(k) == '1' for k in ('OMP_NUM_THREADS','OMP_THREAD_LIMIT','OPENBLAS_NUM_THREADS','VECLIB_MAXIMUM_THREADS')), '四个线程环境变量为1')
    configs = summary['configurations']
    check(len({p['id'] for p in configs}) == len(configs), '配置 id 唯一')
    check(len({p['language'] for p in configs}) == 4, '四语言矩阵')
    for lang in {p['language'] for p in configs}:
        engines = {p['engine'] for p in configs if p['language'] == lang}
        check('tessdata_fast' in engines and any(x.startswith('PP-OCR') for x in engines), '双引擎语言覆盖 ' + lang)
    evaluated = [p for p in configs if p.get('stage') != 'not_evaluated']
    check({p['id'] for p in evaluated} <= {r['configuration_id'] for r in records} <= {p['id'] for p in configs}, '原始记录覆盖已评估配置且残余记录可追踪')
    page_sets = {}
    for p in configs:
        if p.get('stage') == 'not_evaluated':
            check(not p['pareto'], '未评估配置不得进入前沿')
            continue
        check(p.get('stage') in ('screened', 'replicated'), '粗筛阶段明确声明')
        check(not p['pareto'] or p.get('stage') == 'replicated', '最终前沿已重复计时')
        rows = [r for r in records if r['configuration_id'] == p['id']]
        check(len(rows) == 4 and len({r['page_id'] for r in rows}) == 4, '四个唯一页面 ' + p['id'])
        pages = {(r['page_id'], r['input_sha256'], r['page_number']) for r in rows}
        if p['language'] in page_sets:
            check(pages == page_sets[p['language']], '同语言同输入页 ' + p['id'])
        else:
            page_sets[p['language']] = pages
        total_errors = total_chars = 0
        all_ok = True
        times = []
        for r in rows:
            check(r['language'] == p['language'], '记录语言对应')
            check(verify_file(r['input_path'], r['input_sha256']), '记录原件 hash')
            check(len(r['regions']) == 3, '每页三个固定区域')
            repetitions = 3 if p.get('stage') == 'replicated' else 1
            check(len(r['receipts']) >= repetitions and len(r['receipts']) == len(r['durations']), '按阶段核验实际重复次数')
            statuses = [x['status'] for x in r['receipts']]
            ok = all(x in ('ok', 'success', 'succeeded') for x in statuses) and len(statuses) >= repetitions
            all_ok = all_ok and ok and r['eligible']
            if ok:
                check(all(math.isfinite(x) and x > 0 for x in r['durations']), '耗时为正有限数')
                check(same(r['median_seconds'], statistics.median(r['durations'])), '逐页 median 独立重算')
                times.append(r['median_seconds'])
            for index, receipt in enumerate(r['receipts']):
                if receipt['status'] in ('ok', 'success', 'succeeded'):
                    check(same(receipt['total_seconds'], r['durations'][index]), 'receipt 对应实际耗时')
                    check(receipt['total_seconds'] >= receipt['render_seconds'] + receipt['ocr_seconds'] - 1e-9, '端到端包含渲染和 OCR')
                    check(receipt['width'] > 0 and receipt['height'] > 0, '渲染尺寸有效')
                    check(bool(receipt['files']), '逐次输出落盘')
                    for path, digest in receipt['files'].items():
                        check(verify_file(path, digest), 'receipt 文件 hash: ' + path)
            first = r['receipts'][0] if r['receipts'] else {}
            word_paths = [path for path in first.get('files', {}) if path.endswith('words.json')]
            words = json.loads((base / word_paths[0]).read_text()) if word_paths else None
            check(words is not None or not r['eligible'], '成功页面提供原始 words.json')
            for region in r['regions']:
                expected_region = gold_regions.get(region['id'])
                check(expected_region is not None and normalize(expected_region['reference'], r['language']) == normalize(region['gold'], r['language']) and expected_region['bbox_normalized'] == region['bbox_normalized'], '原始人工真值与固定框未更换')
                if words is not None:
                    x0, y0, x1, y1 = region['bbox_normalized']
                    indices = [i for i, word in enumerate(words) if x0 <= (word['bbox'][0] + word['bbox'][2]) / (2 * first['width']) <= x1 and y0 <= (word['bbox'][1] + word['bbox'][3]) / (2 * first['height']) <= y1]
                    rebuilt = ''.join(words[i].get('separator', ' ') + words[i]['text'] for i in indices)
                    check(indices == region['selected_word_indices'], '几何中心选词及原始顺序独立重算')
                    check(normalize(rebuilt, r['language']) == normalize(region['actual'], r['language']), 'separator 保留子词空白后重建 actual')
                gold = normalize(region['gold'], r['language'])
                actual = normalize(region['actual'], r['language'])
                err = distance(gold, actual)
                check(bool(gold), '区域真值非空')
                check(region['reference_characters'] == len(gold) and region['errors'] == err, '区域字符数和编辑距离独立重算')
                box = region['bbox_normalized']
                check(len(box) == 4 and all(0 <= n <= 1 for n in box) and box[0] < box[2] and box[1] < box[3], '固定区域范围有效')
                total_errors += err
                total_chars += len(gold)
        check(p['eligible'] == all_ok, '失败配置不参与前沿 ' + p['id'])
        check(p['errors'] == total_errors and p['reference_characters'] == total_chars, '配置字符加权分子分母')
        check(total_chars > 0 and same(p['cer'], total_errors / total_chars), '配置 CER 独立重算 ' + p['id'])
        if p['eligible']:
            check(same(p['total_seconds'], sum(times)), '逐页 median 求和 ' + p['id'])
        dominated = any(q.get('stage') != 'not_evaluated' and q['eligible'] and q['language'] == p['language'] and q['cer'] <= p['cer'] and q['total_seconds'] <= p['total_seconds'] and (q['cer'] < p['cer'] or q['total_seconds'] < p['total_seconds']) for q in configs)
        check(p['pareto'] == (p['eligible'] and not dominated), '严格 Pareto 独立重算 ' + p['id'])
    report = {'passed': all(x['passed'] for x in checks), 'assertions': checks,
              'command': 'python3 tests/paddle_ocr_independent_acceptance.py --experiment ' + str(args.experiment),
              'source_verification': {
                  'mode': 'explicit_historical_snapshot' if args.source_snapshot is not None else 'current_experiment_source',
                  'root': str(source_base),
                  'scope': 'Only source_files in manifest, screening-manifest and entry-receipt use this root; input/model/receipt hashes use the experiment root',
                  'limitation': 'Historical source validation does not establish that current source equals the source used by historical timings' if args.source_snapshot is not None else 'Current source hashes must equal historical frozen source hashes',
                  'fallback': False,
              },
              'manual_review_evidence': 'artifacts/paddle-ocr-independent-20261019/manual-review.json',
              'verification_boundaries': ['人工原页核对覆盖越南语12区域及典型简中/英语页面，并非所有真值逐字重核', '已核线程声明与配置，未进行系统级CPU线程跟踪', '已核官方当前版本及本地模型hash，未重新下载第二份远端模型字节比较', '凭据扫描为已列pattern，不能证明任意秘密不存在']}
    if args.source_snapshot is not None:
        report['command'] += ' --source-snapshot ' + str(args.source_snapshot)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"{sum(x['passed'] for x in checks)}/{len(checks)} passed: {args.out}")
    raise SystemExit(0 if report['passed'] else 1)

if __name__ == '__main__':
    main()
