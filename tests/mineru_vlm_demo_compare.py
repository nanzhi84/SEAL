"""Offline acceptance of externally produced MinerU VLM artifacts.

Run: python3 tests/mineru_vlm_demo_compare.py
Requires saved result/*.md, result/*.json and the reviewed first-paragraph truth.
No service calls, credentials, or product imports. Outputs only acceptance artifacts.
"""
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/mineru-vlm-vietnam-20261016'
RESULT = OUT / 'result'


def normalize(value):
    return ' '.join(unicodedata.normalize('NFC', value).split())


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        nxt = [i]
        for j, y in enumerate(b, 1):
            nxt.append(min(row[j] + 1, nxt[-1] + 1, row[j-1] + (x != y)))
        row = nxt
    return row[-1]


def json_evidence(value, path='$'):
    """Record explicit page counts and page-index-bearing external objects."""
    findings = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f'{path}.{key}'
            if key in ('page_count', 'total_pages', 'num_pages', 'pages_count') and isinstance(item, int):
                findings.append({'path': here, 'kind': 'declared_page_count', 'value': item})
            if key in ('page_idx', 'page_index', 'page_no', 'page_number') and isinstance(item, int):
                findings.append({'path': here, 'kind': key, 'value': item})
            if key == 'pdf_info' and isinstance(item, list):
                findings.append({'path': here, 'kind': 'pdf_info_length', 'value': len(item)})
            findings.extend(json_evidence(item, here))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            findings.extend(json_evidence(item, f'{path}[{i}]'))
    return findings


def main():
    files = sorted(RESULT.rglob('*.md'))
    if not files:
        raise SystemExit('WAITING: result/*.md 尚未产生；未生成验收结论。')
    truth = normalize((ROOT / 'artifacts/mineru-vietnam-20261016/ground-truth-first-paragraph.txt').read_text())
    assert len(truth) == 243, 'Reviewed truth contract changed'
    samples = []
    for file in files:
        text = file.read_text()
        paragraphs = [normalize(p) for p in re.split(r'\n\s*\n', text) if '136/2024/QH15' in p]
        if not paragraphs:
            continue
        paragraph = paragraphs[0]
        edits = distance(truth, paragraph)
        samples.append({'file': str(file.relative_to(ROOT)), 'normalized_paragraph': paragraph,
                        'reference_characters': len(truth), 'recognized_characters': len(paragraph),
                        'edit_distance': edits, 'cer': edits / len(truth),
                        'boundary_complete': paragraph.startswith('Nghị quyết số') and paragraph.endswith(':'),
                        'act_number_preserved': paragraph.count('136/2024/QH15') == 1,
                        'dates_preserved': all(x in paragraph for x in ('26 tháng 6 năm 2024', '01 tháng 01 năm 2025'))})
    evidence = []
    for file in sorted(RESULT.rglob('*.json')):
        try:
            obj = json.loads(file.read_text())
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        evidence.append({'file': str(file.relative_to(ROOT)), 'page_evidence': json_evidence(obj)})
    layout = json.loads((RESULT / 'layout.json').read_text())
    pages = layout.get('pdf_info', [])
    indices = [p.get('page_idx') for p in pages]
    page_coverage = len(pages) == 36 and sorted(indices) == list(range(36))
    content_file = next(RESULT.glob('*_content_list.json'))
    content = json.loads(content_file.read_text())
    content_indices = sorted({p.get('page_idx') for p in content if isinstance(p.get('page_idx'), int)})
    coverage_ok = page_coverage and content_indices == list(range(36))
    coverage_result = ('PASS: 36 page records covering 0..35; visible-content completeness unverified'
                       if coverage_ok else 'FAIL: layout or content-list does not cover all 36 pages')
    first_page = [p for p in content if p.get('page_idx') == 0]
    title = 'NGHỊ QUYẾT Về tổ chức chính quyền đô thị và thí điểm một số cơ chế, chính sách đặc thù phát triển thành phố Đà Nẵng'
    footnotes = [p['text'] for p in first_page if p.get('type') == 'page_footnote']
    headers = [p['text'] for p in first_page if p.get('type') == 'header']
    markdown = (RESULT / 'full.md').read_text()
    visual = {
        'source': 'artifacts/mineru-vietnam-20261016/original-page-1.png',
        'review': 'Independent agent visually compared original page 1 with saved output on 2026-10-16.',
        'main_title_matches_image': any(normalize(p.get('text', '')) == title for p in first_page),
        'observations': 'Main title wording and Vietnamese accents match the original image. Three page headers and six footnote blocks are present in content-list/layout JSON, but absent from full.md. Footnote act numbers 71/2025/QH15, 72/2025/QH15 and 203/2025/QH15 are retained in JSON. Original curved quotation marks are inconsistently converted to straight quotes. Footnote marker is represented as a TeX superscript in JSON; body marker is Unicode superscript. Source footnote visibly repeats số số and JSON retains it.',
        'headers': headers, 'footnotes': footnotes,
        'footnotes_in_markdown': any('có căn cứ ban hành như sau' in p for p in [markdown]),
        'scope': 'Visual sample only; not a full-page or full-document CER measurement.'}
    assertions = {'json_layout_has_36_distinct_pages': page_coverage,
                  'content_list_covers_page_indices_0_to_35': content_indices == list(range(36)),
                  'reviewed_truth_243_characters': len(truth) == 243,
                  'first_paragraph_found': bool(samples),
                  'paragraph_boundaries_complete': bool(samples) and all(x['boundary_complete'] for x in samples),
                  'original_act_number_preserved': bool(samples) and all(x['act_number_preserved'] for x in samples),
                  'both_dates_preserved': bool(samples) and all(x['dates_preserved'] for x in samples)}
    report = {'scope': 'Saved VLM PDF artifacts; first paragraph character scoring only',
              'normalization': 'Unicode NFC, collapse all whitespace, trim; punctuation/case/spaces count',
              'assertions': assertions, 'samples': samples, 'json_page_evidence': evidence,
              'page_coverage': {'layout_records': len(pages), 'layout_page_indices': indices, 'content_list_page_indices': content_indices, 'result': coverage_result},
              'full_36_page_completeness': 'FAIL for faithful all-visible-content Markdown preservation: page-1 headers and footnotes omitted; full-document text accuracy UNVERIFIED',
              'visual_title_footnote': visual,
              'limitations': ['PDF server rendering, resolution and preprocessing unknown; not same-condition comparison to 144 DPI image input.',
                              'Nonempty Markdown does not prove complete 36-page extraction.',
                              'No accuracy threshold predefined; assertion PASS does not mean business accuracy accepted.']}
    (OUT / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    lines = ['# MinerU VLM 越南 PDF 工件独立验收', '',
             '复现：`python3 tests/mineru_vlm_demo_compare.py`。Python 3 标准库；需已保存 result/ 下 Markdown、JSON 及人工核对首段真值。不联网，不读凭据，不修改源工件。', '',
             'NFC、合并空白后真值为243字符；CER为字符Levenshtein距离/243，大小写、标点与空格参与计数。', '',
             '| 工件 | 字符数 | 编辑距离 | CER |', '|---|---:|---:|---:|']
    for sample in samples:
        lines.append(f"| {sample['file']} | {sample['recognized_characters']} | {sample['edit_distance']} | {sample['cer']:.6%} |")
    lines += ['', '断言：'] + [f"- {k}: {'PASS' if v else 'FAIL'}" for k, v in assertions.items()]
    lines += ['', '页覆盖：' + coverage_result + '。页记录不证明每页所有可见内容完整；全篇文本精度 UNVERIFIED。',
              '', '已肉眼比较原图第1页：主标题文字、越南语声调与输出一致。JSON保留3个页眉和6个脚注块，含71/2025/QH15、72/2025/QH15、203/2025/QH15；full.md没有这些页眉和脚注正文，仅保留正文上标¹。引号有弯引号转直引号，脚注编号在JSON用TeX上标。原图脚注自身重复 số số，JSON照录。此抽样不等于全页CER或全篇验收。', '',
              '限制：PDF服务端渲染、分辨率和预处理未知，与144 DPI同图输入不是完全同条件。仅首段CER不能代表全篇精度，PASS仅表示列出的有限断言成立，未设置业务精度阈值。']
    (OUT / 'acceptance.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'assertions': assertions, 'samples': [{k:v for k,v in x.items() if k != 'normalized_paragraph'} for x in samples]}, ensure_ascii=False))
    raise SystemExit(0 if all(assertions.values()) else 1)


if __name__ == '__main__':
    main()
