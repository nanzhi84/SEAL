#!/usr/bin/env python3
"""Serialized real-scan region CER benchmark; no product code dependency."""
import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'artifacts/tesseract-multilang-20261016'
OUT = BASE / 'benchmark'
WORK = ROOT / 'work/tesseract-multilang-20261016'
LANGS = ('chi_sim', 'chi_tra', 'eng')
DPIS = (96, 120, 144, 200, 240, 300)
CONTRACT = {
    'version': 1,
    'selection': 'TSV level 5 word bbox centre inside inclusive normalized rectangle; original TSV hierarchy/output order; no confidence filtering',
    'normalization': 'NFC; Chinese remove all whitespace; English collapse whitespace; preserve case punctuation digits and script forms',
    'metric': 'global Levenshtein entire fixed region; sum edit distances / sum gold characters; no substring search',
    'timing': 'per page reopen PDF, render PNG and close all resources, then OCR process including txt+tsv writes; exclude scoring/download',
    'eligibility': '4 successful pages, 12 scored regions, identical gold denominator; blank whole-page txt or missing/empty TSV fails',
}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def norm(text, lang):
    text = unicodedata.normalize('NFC', text)
    return ' '.join(text.split()) if lang == 'eng' else ''.join(text.split())


def distance(gold, actual):
    row = list(range(len(actual) + 1))
    for i, a in enumerate(gold, 1):
        new = [i]
        for j, b in enumerate(actual, 1):
            new.append(min(row[j] + 1, new[-1] + 1, row[j-1] + (a != b)))
        row = new
    return row[-1]


def extract(tsv, bbox, width, height, lang):
    chosen = []
    for index, word in enumerate(csv.DictReader(io.StringIO(tsv), delimiter='\t', quoting=csv.QUOTE_NONE)):
        if word['level'] != '5':
            continue
        x = (int(word['left']) + int(word['width']) / 2) / width
        y = (int(word['top']) + int(word['height']) / 2) / height
        if bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]:
            chosen.append({'tsv_row': index + 2, **word, 'centre_normalized': [x, y]})
    return norm(' '.join(w['text'] for w in chosen), lang), chosen


def selfcheck():
    # Physical selection must reject an identical passage outside the region.
    header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    fixture = header + '5\t1\t1\t1\t1\t1\t10\t10\t10\t10\t-1\tBAD\n5\t1\t2\t1\t1\t1\t70\t70\t10\t10\t99\tGOLD\n'
    actual, words = extract(fixture, [0, 0, .5, .5], 100, 100, 'eng')
    quote_fixture = header + '5\t1\t1\t1\t1\t1\t10\t10\t10\t10\t-1\t"Hello\n5\t1\t1\t1\t1\t2\t20\t10\t10\t10\t99\tworld"\n'
    quoted, quote_words = extract(quote_fixture, [0, 0, 1, 1], 100, 100, 'eng')
    checks = {
        'literal_tsv_quotes_no_swallowed_rows': quoted == '"Hello world"' and len(quote_words) == 2,
        'physical_selection_repeated_passage': actual == 'BAD' and len(words) == 1,
        'low_confidence_retained': words[0]['conf'] == '-1',
        'missing_characters': distance('abcd', '') == 4,
        'extra_characters': distance('abc', 'abcXYZ') == 3,
        'global_no_substring': distance('abc', 'XYZabcXYZ') == 6,
        'case_and_punctuation': distance(norm('A。', 'chi_sim'), norm('a.', 'chi_sim')) == 2,
        'chinese_nfc_whitespace': norm('中 \n 文e\u0301', 'chi_sim') == '中文é',
        'english_whitespace': norm('A\n B  C', 'eng') == 'A B C',
        'script_preserved': distance(norm('臺', 'chi_tra'), norm('台', 'chi_tra')) == 1,
        'whole_region_repetition': distance('abc', 'abcabc') == 3,
    }
    assert all(checks.values()), checks
    return checks


def freeze():
    files = [BASE / 'boundaries.json']
    references = {}
    for lang in LANGS:
        files += [BASE / lang / 'references.json', BASE / lang / 'manifest.json']
        refs = json.loads((BASE / lang / 'references.json').read_text())
        assert len(refs['pages']) == 4, lang
        assert sum(len(p['regions']) for p in refs['pages']) == 12, lang
        assert len({r['id'] for p in refs['pages'] for r in p['regions']}) == 12
        for page in refs['pages']:
            files.append(ROOT / page['pdf_path'])
            files.append(ROOT / page['image_path'])
            for region in page['regions']:
                box = region['bbox_normalized']
                assert 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1
                assert norm(region['reference'], lang)
        references[lang] = refs
        for family in ('fast', 'best'):
            files.append(WORK / 'models' / family / (lang + '.traineddata'))
    import pypdfium2
    import PIL
    frozen = {
        'contract': CONTRACT, 'hashes': {str(p.resolve()): digest(p) for p in set(files)},
        'runner_sha256': digest(__file__), 'references': references,
        'runtime': {'python': platform.python_version(), 'platform': platform.platform(),
                    'pypdfium2': str(pypdfium2.PYPDFIUM_INFO), 'pdfium': str(pypdfium2.PDFIUM_INFO),
                    'pillow': PIL.__version__, 'tesseract_binary_sha256': digest(__import__('shutil').which('tesseract')),
                    'tesseract': subprocess.check_output(['tesseract', '--version'], stderr=subprocess.STDOUT, text=True)},
    }
    return frozen


def run_page(lang, family, dpi, page, index, frozen):
    import pypdfium2 as pdfium
    folder = OUT / lang / family / str(dpi) / f'page-{index + 1}'
    folder.mkdir(parents=True, exist_ok=True)
    png = WORK / 'images' / lang / family / str(dpi) / f'page-{index + 1}.png'
    png.parent.mkdir(parents=True, exist_ok=True)
    modeldir = WORK / 'models' / family
    command = ['tesseract', str(png), str(folder / 'text'), '--tessdata-dir', str(modeldir),
               '-l', lang, '--oem', '1', '--psm', '3', '--dpi', str(dpi),
               '-c', 'tessedit_create_txt=1', '-c', 'tessedit_create_tsv=1']
    record = {'page_id': f"{page['document_id']}:p{page['page_number']}", 'command': command,
              'language': lang, 'family': family, 'dpi': dpi, 'page_index': index,
              'model_sha256': digest(modeldir / (lang + '.traineddata')),
              'input_sha256': digest(ROOT / page['pdf_path']), 'environment': {'OMP_THREAD_LIMIT': '1'}}
    start = time.perf_counter()
    try:
        doc = pdfium.PdfDocument(str(ROOT / page['pdf_path']))
        try:
            pdfpage = doc[page['page_number'] - 1]
            try:
                bitmap = pdfpage.render(scale=dpi / 72)
                try:
                    image = bitmap.to_pil()
                    try:
                        record['width'], record['height'] = image.size
                        image.save(png, format='PNG')
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                pdfpage.close()
        finally:
            doc.close()
        render_end = time.perf_counter()
        proc = subprocess.run(command, env={**os.environ, 'OMP_THREAD_LIMIT': '1'}, capture_output=True, text=True, timeout=600)
        end = time.perf_counter()
        record.update(returncode=proc.returncode, render_seconds=render_end-start,
                      ocr_seconds=end-render_end, total_seconds=end-start,
                      stdout=proc.stdout, stderr=proc.stderr)
        record['success'] = (proc.returncode == 0 and (folder / 'text.txt').exists()
                             and bool((folder / 'text.txt').read_text().strip())
                             and (folder / 'text.tsv').exists() and (folder / 'text.tsv').stat().st_size > 0)
    except Exception as exc:
        record.update(success=False, total_seconds=time.perf_counter()-start, error=repr(exc), returncode=None)
    record['outputs_sha256'] = {name: digest(folder / name) for name in ('text.txt', 'text.tsv') if (folder / name).exists()}
    dump(folder / 'receipt.json', record)
    return record


def score(frozen):
    results = []
    for lang in LANGS:
        pages = frozen['references'][lang]['pages']
        denominator = sum(len(norm(r['reference'], lang)) for p in pages for r in p['regions'])
        for family in ('fast', 'best'):
            for dpi in DPIS:
                rows, records = [], []
                for index, page in enumerate(pages):
                    folder = OUT / lang / family / str(dpi) / f'page-{index+1}'
                    receipt = folder / 'receipt.json'
                    if not receipt.exists():
                        continue
                    record = json.loads(receipt.read_text())
                    records.append(record)
                    if not record['success']:
                        continue
                    assert all(digest(folder / name) == sha for name, sha in record['outputs_sha256'].items()), 'OCR output drift'
                    for region in page['regions']:
                        actual, words = extract((folder / 'text.tsv').read_text(), region['bbox_normalized'], record['width'], record['height'], lang)
                        gold = norm(region['reference'], lang)
                        rows.append({'id': region['id'], 'bbox_normalized': region['bbox_normalized'],
                                     'gold': gold, 'actual': actual, 'gold_characters': len(gold),
                                     'errors': distance(gold, actual), 'selected_words': words})
                eligible = len(records) == 4 and all(r['success'] for r in records) and len(rows) == 12 and sum(r['gold_characters'] for r in rows) == denominator
                result = {'language': lang, 'family': family, 'dpi': dpi, 'eligible': eligible,
                          'successful_pages': sum(r['success'] for r in records), 'regions': len(rows),
                          'gold_characters': denominator, 'errors': sum(r['errors'] for r in rows),
                          'total_seconds': sum(r['total_seconds'] for r in records)}
                result['cer'] = result['errors'] / denominator if eligible else None
                dump(OUT / lang / family / str(dpi) / 'scores.json', {'summary': result, 'regions': rows})
                results.append(result)
    for result in results:
        result['pareto'] = result['eligible'] and not any(
            other['eligible'] and other['language'] == result['language']
            and other['total_seconds'] <= result['total_seconds'] and other['cer'] <= result['cer']
            and (other['total_seconds'] < result['total_seconds'] or other['cer'] < result['cer'])
            for other in results)
    dump(OUT / 'summary.json', results)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--score-only', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    dump(OUT / 'scoring-contract.json', {'contract': CONTRACT, 'behavior_assertions': selfcheck()})
    current = freeze()
    frozen_path = OUT / 'frozen-inputs.json'
    if frozen_path.exists():
        frozen = json.loads(frozen_path.read_text())
        if (OUT / 'scoring-revision.json').exists():
            revision = json.loads((OUT / 'scoring-revision.json').read_text())
            assert frozen['runner_sha256'] == revision['timing_runner_sha256']
            assert current['runner_sha256'] == revision['scoring_runner_sha256']
            comparable = dict(current, runner_sha256=frozen['runner_sha256'])
            assert frozen == comparable, 'Frozen input/model/runtime drift'
        else:
            assert frozen == current, 'Frozen input/model/runner/runtime drift; use a new run directory'
    else:
        assert not args.score_only, 'No frozen run'
        dump(frozen_path, current)
        frozen = current
    if args.prepare_only:
        print('PREPARED: contract checks passed; no timed OCR run')
        return
    if not args.score_only:
        for lang in LANGS:
            for family in ('fast', 'best'):
                for dpi in DPIS:
                    for index, page in enumerate(frozen['references'][lang]['pages']):
                        receipt = OUT / lang / family / str(dpi) / f'page-{index+1}' / 'receipt.json'
                        if args.resume and receipt.exists() and json.loads(receipt.read_text())['success']:
                            continue
                        if receipt.exists() and not args.resume:
                            raise RuntimeError('Existing outputs: use --resume')
                        record = run_page(lang, family, dpi, page, index, frozen)
                        print(lang, family, dpi, index+1, record['success'], round(record['total_seconds'], 3), flush=True)
    results = score(frozen)
    print('Eligible:', sum(r['eligible'] for r in results), '/ 36')


if __name__ == '__main__':
    main()
