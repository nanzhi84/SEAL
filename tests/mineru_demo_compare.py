"""Reproduce artifact-only first-paragraph OCR acceptance; no service required."""
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/mineru-vietnam-20261016'

def normalize(text):
    return ' '.join(unicodedata.normalize('NFC', text).split())

def compare(ref, hyp):
    dp = [[0] * (len(hyp) + 1) for _ in range(len(ref) + 1)]
    for i in range(len(ref) + 1):
        dp[i][0] = i
    for j in range(len(hyp) + 1):
        dp[0][j] = j
    for i, a in enumerate(ref, 1):
        for j, b in enumerate(hyp, 1):
            dp[i][j] = min(dp[i-1][j]+1, dp[i][j-1]+1, dp[i-1][j-1]+(a != b))
    i, j = len(ref), len(hyp)
    operations = []
    while i or j:
        if i and j and dp[i][j] == dp[i-1][j-1] + (ref[i-1] != hyp[j-1]):
            if ref[i-1] != hyp[j-1]:
                operations.append({'operation': 'substitution', 'reference_offset': i-1, 'reference': ref[i-1], 'recognized': hyp[j-1]})
            i, j = i-1, j-1
        elif i and dp[i][j] == dp[i-1][j] + 1:
            operations.append({'operation': 'deletion', 'reference_offset': i-1, 'reference': ref[i-1], 'recognized': ''})
            i -= 1
        else:
            operations.append({'operation': 'insertion', 'reference_offset': i, 'reference': '', 'recognized': hyp[j-1]})
            j -= 1
    operations.reverse()
    return {'reference_characters': len(ref), 'recognized_characters': len(hyp), 'edit_distance': dp[-1][-1], 'cer': dp[-1][-1]/len(ref), 'operations': operations}

ref = normalize((OUT / 'ground-truth-first-paragraph.txt').read_text())
inputs = {
    'existing_ocr': ROOT / 'artifacts/ocr-vietnam-review-20261016/page-1.txt',
    'mineru_pipeline': OUT / 'first-page-image.md',
    'mineru_pdf_pages_01_10': OUT / 'pages-01-10.md',
}
results = {}
for name, path in inputs.items():
    text = path.read_text()
    # First body paragraph is the unique paragraph containing the original act number.
    matches = [p for p in re.split(r'\n\s*\n', text) if '136/2024/QH15' in p]
    paragraph = normalize(matches[0])
    assert paragraph.endswith(':'), f'Paragraph boundary missing: {name}'
    assert paragraph.count('136/2024/QH15') == 1
    result = compare(ref, paragraph)
    result['normalized_paragraph'] = paragraph
    result['act_number_preserved'] = '136/2024/QH15' in paragraph
    result['dates_preserved'] = all(s in paragraph for s in ('26 tháng 6 năm 2024', '01 tháng 01 năm 2025'))
    results[name] = result
assert results['existing_ocr']['edit_distance'] == 0
assert results['mineru_pipeline']['edit_distance'] > 0
assert results['mineru_pdf_pages_01_10']['edit_distance'] > 0
assert all(r['act_number_preserved'] and r['dates_preserved'] for r in results.values())
(OUT / 'first-paragraph-comparison.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
for name, result in results.items():
    print(f"{name}: {result['edit_distance']}/{result['reference_characters']} CER={result['cer']:.6%}")
print('PASS: boundaries, original act number, dates, existing OCR exact match, MinerU image and PDF mismatches')
