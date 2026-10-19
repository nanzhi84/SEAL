"""Generate reproducible benchmark report and per-document Pareto recommendations."""
import json
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).resolve().parents[1] / 'artifacts/tesseract-multilang-20261016'
OUT = BASE / 'benchmark'
summary = json.loads((OUT / 'summary.json').read_text())
frozen = json.loads((OUT / 'frozen-inputs.json').read_text())
lines = ['# Tesseract 三语言真实扫描 benchmark', '',
         '本次为固定矩形片段 CER，分母为人工逐字转写的区域字符总数；不是全页或全语言准确率。全局 Levenshtein 包含缺字、多字、大小写、标点及文字形式差异。', '',
         '计时包含每页重新打开 PDF、渲染 PNG、关闭资源、OCR 进程及 txt/tsv 落盘；排除下载及离线评分。OEM=1，PSM=3，OMP_THREAD_LIMIT=1，全串行。单次计时无重复统计，不支持微小差异的稳定性推断。', '',
         '## 语言配置与 Pareto', '', '|语言|模型|DPI|成功页|区域数|字符分母|错误数|CER|总秒|Pareto|', '|---|---|---:|---:|---:|---:|---:|---:|---:|---|']
for row in summary:
    cer = f"{row['cer']:.2%}" if row['eligible'] else 'FAIL'
    lines.append(f"|{row['language']}|{row['family']}|{row['dpi']}|{row['successful_pages']}|{row['regions']}|{row['gold_characters']}|{row['errors']}|{cer}|{row['total_seconds']:.3f}|{row['pareto']}|")
source_results = []
for lang, references in frozen['references'].items():
    page_ids = defaultdict(list)
    region_sources = {}
    for index, page in enumerate(references['pages']):
        page_ids[page['document_id']].append(index + 1)
        for region in page['regions']:
            region_sources[region['id']] = page['document_id']
    for row in summary:
        if row['language'] != lang or not row['eligible']:
            continue
        folder = OUT / lang / row['family'] / str(row['dpi'])
        scores = json.loads((folder / 'scores.json').read_text())
        for source, indices in page_ids.items():
            regions = [r for r in scores['regions'] if region_sources[r['id']] == source]
            receipts = [json.loads((folder / f'page-{i}' / 'receipt.json').read_text()) for i in indices]
            errors = sum(r['errors'] for r in regions)
            denominator = sum(r['gold_characters'] for r in regions)
            source_results.append({'language': lang, 'source': source, 'family': row['family'], 'dpi': row['dpi'], 'errors': errors, 'gold_characters': denominator, 'cer': errors/denominator, 'total_seconds': sum(r['total_seconds'] for r in receipts)})
for row in source_results:
    peers = [p for p in source_results if p['source'] == row['source'] and p['language'] == row['language']]
    row['pareto'] = not any(p['cer'] <= row['cer'] and p['total_seconds'] <= row['total_seconds'] and (p['cer'] < row['cer'] or p['total_seconds'] < row['total_seconds']) for p in peers)
lines += ['', '## 逐来源配置建议', '', '分别列出实测速度最快、片段 CER 最低及 Pareto 备选；按业务误差容忍度选择，不自动修改生产配置。']
for lang, references in frozen['references'].items():
    for source in dict.fromkeys(p['document_id'] for p in references['pages']):
        peers = [r for r in source_results if r['language'] == lang and r['source'] == source]
        if not peers:
            lines += ['', f'- {lang}/{source}: 无完整成功配置，无法建议。']
            continue
        fastest = min(peers, key=lambda r: (r['total_seconds'], r['cer']))
        accurate = min(peers, key=lambda r: (r['cer'], r['total_seconds']))
        def label(r):
            return f"{r['family']} {r['dpi']} DPI（CER {r['cer']:.2%}，{r['total_seconds']:.3f}秒）"
        lines += ['', f"- {lang}/{source}：速度优先 {label(fastest)}；片段精度优先 {label(accurate)}。Pareto：" + '；'.join(label(r) for r in peers if r['pareto']) + '。']
lines += ['', '## 范围与复现', '', '- 共12页、36个人工固定区域，每语言4页12区域；chi_tra仅1份文书，其余语言各2份，语料覆盖不足。横排为主，不推断直排、手写、印章或全页召回。', '- source manifest/references、输入图/PDF、模型、runner与运行库版本均冻结于 frozen-inputs.json；任何变化拒绝混跑。', '- 每页 receipt.json 包含命令、hash、returncode、耗时；text.txt/text.tsv 为完整OCR输出；scores.json 含逐区域gold、提取文本及所有选词明细。', '- 环境：项目 .venv，pypdfium2/Pillow，系统 tesseract，6个fast/best模型；输入与模型需符合 frozen-inputs.json。', '', '```sh', '.venv/bin/python tests/tesseract_multilang_benchmark.py --prepare-only', '.venv/bin/python tests/tesseract_multilang_benchmark.py --resume', '.venv/bin/python tests/tesseract_multilang_benchmark.py --score-only', '.venv/bin/python tests/tesseract_multilang_report.py', '```', '', '历史计时复用请用 --score-only；重新实际计时需独立新运行目录，避免混入旧结果。本次只执行单次矩阵，不含重复试验置信区间。']
(OUT / 'source-summary.json').write_text(json.dumps(source_results, ensure_ascii=False, indent=2) + '\n')
(OUT / 'report.md').write_text('\n'.join(lines) + '\n')
print(OUT / 'report.md')
