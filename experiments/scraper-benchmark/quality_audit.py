#!/usr/bin/env python3
"""Independent audit of archived real documents, never grades one engine by another.

HTML oracle: manually reviewed raw section boundaries, stdlib HTMLParser, head
<title>, and annotated source dates; no Recipe selectors or extractor imports.
PDF oracle: Poppler pdftotext -layout, independent of pypdf, plus five page PNGs
visually inspected during this run. Not a benchmark of statistical accuracy.
"""
import html
import json
import re
import subprocess
from html.parser import HTMLParser
from urllib.parse import urljoin

from bench.common import HERE, RAW, RESULTS, read_json, sha256_hex, write_json


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def compact(s):
    return re.sub(r'\s+', '', s)


def body_fragment(raw, marker):
    """Byte-source labelled div boundary, independent of CSS/XPath extraction."""
    start = raw.index(marker)
    depth = 0
    for match in re.finditer(r'<div\b[^>]*>|</div\s*>', raw[start:], re.I):
        depth += -1 if match.group().startswith('</') else 1
        if depth == 0:
            return raw[start:start + match.end()]
    raise ValueError('unbalanced labelled body')


def detail_audit(expected):
    rows = []
    for tid, date in expected['detail_dates'].items():
        raw = (RAW / f'{tid}.raw.html').read_text()
        marker = ('<div class="txt_txt">' if tid.startswith('A') else
                  '<div class="vF_detail_content">' if tid.startswith('B') else
                  '<div id="fontzoom">')
        fragment = body_fragment(raw, marker)
        parser = Text()
        parser.feed(fragment)
        truth = ''.join(parser.parts)
        title = html.unescape(re.search(r'<title>(.*?)</title>', raw, re.S).group(1))
        title = re.split(r' - 中华人民共和国最高人民法院|_中国政府采购网|_中华人民共和国最高人民检察院', title)[0]
        doc = read_json(RESULTS / 'replay' / f'{tid}.raw.json')
        live = read_json(RESULTS / 'parsed' / f'{tid}.raw.json')
        generic = live['_generic_trafilatura']
        segments = [compact(t) for t in parser.parts if len(compact(t)) >= 8]
        gt = compact(generic.get('body_text') or '')
        attachment_truth = sorted(set(urljoin(doc['source_url'], u) for u in
                                      re.findall(r'href=[\'"]([^\'"]+)[\'"]', raw)
                                      if re.search(r'\.(pdf|docx?|xlsx?|txt)(?:\?|$)', u, re.I)))
        record = {'target': tid, 'title_exact': title == doc['title'],
                  'date_exact': date == doc['published_at'] and date in raw,
                  'body_exact': compact(truth) == compact(doc['body_text']),
                  'body_chars_non_whitespace': len(compact(truth)),
                  'title_oracle': title, 'date_oracle': date, 'body_marker': marker,
                  'body_oracle_sha256': sha256_hex(compact(truth).encode()),
                  'raw_sha256': sha256_hex((RAW / f'{tid}.raw.html').read_bytes()),
                  'attachments_exact': attachment_truth == sorted(a['url'] for a in doc['attachments']),
                  'attachments_expected': len(attachment_truth),
                  'generic': {'title_exact': generic['title'] == title,
                              'date_exact': generic['published_at'] == date,
                              'body_exact': gt == compact(truth),
                              'expected_segments': len(segments),
                              'segments_present': sum(s in gt for s in segments),
                              'body_chars': len(generic.get('body_text') or '')}}
        rows.append(record)
    return rows


def discovery_audit():
    # Regex over hrefs is a different extraction path from the CSS recipe.
    pages = []
    expected_all, found_all = set(), set()
    for name in ['A_list_p1', 'A_list_p1__p2']:
        raw = (RAW / f'{name}.raw.html').read_text()
        doc = read_json(RESULTS / 'replay' / f'{name}.raw.json')
        paths = re.findall(r'href=[\'"]([^\'"]*/shenpan/xiangqing/\d+\.html)[\'"]', raw)
        expected = set(urljoin(doc['source_url'], u) for u in paths)
        found = {d['url'] for d in doc['document_links']}
        expected_all |= expected
        found_all |= found
        pages.append({'page': doc['source_url'], 'expected_urls': sorted(expected),
                      'found_urls': sorted(found), 'missing': sorted(expected - found),
                      'unexpected': sorted(found - expected)})
    first = read_json(RESULTS / 'replay/A_list_p1.raw.json')
    second = read_json(RESULTS / 'replay/A_list_p1__p2.raw.json')
    edges = []
    for i in range(1, 6):
        detail = read_json(RESULTS / f'replay/A_detail_{i}.raw.json')
        verified = detail['source_url'] in pages[0]['expected_urls']
        edges.append({'parent_url': first['source_url'], 'detail_url': detail['source_url'],
                      'verified_in_raw': verified,
                      'live_scheduling': 'preselected target, NOT dynamically scheduled from discovery'})
    write_json(RESULTS / 'discovery_evidence.json', {'pages': pages, 'edges': edges})
    return {'expected': len(expected_all), 'found': len(found_all),
            'matched': len(expected_all & found_all),
            'coverage': len(expected_all & found_all) / len(expected_all),
            'parent_edges': sum(e['verified_in_raw'] for e in edges),
            'pagination_url_correct': first['next_page'] == second['source_url'],
            'scope': 'two archived pages only; whole-site discovery UNKNOWN',
            'live_discovery_to_fetch_e2e': 'NOT_PROVEN; original details were seed targets'}


def poppler_rows(text):
    rows = []
    for line in text.splitlines():
        if 'IFPI' not in line:
            continue
        left, sid = line.split('IFPI', 1)
        number, name = left.strip().split(maxsplit=1)
        if number.isdigit():
            rows.append([number, name.strip(), 'IFPI ' + sid.strip()])
    return rows


def pdf_audit(expected):
    textfile = RESULTS / 'pdf-poppler.txt'
    subprocess.run(['pdftotext', '-layout', str(RAW / 'C_pdf.raw.pdf'), str(textfile)], check=True)
    text = textfile.read_text()
    oracle = poppler_rows(text)
    # Serialized pypdf + SEAL table rows, not imported implementation under test.
    parsed = read_json(RESULTS / 'parsed/C_pdf.pdfparse.json')
    # These are the actual full recovered rows from the independently run parser.
    table = read_json(RESULTS / 'pdf_table_rows.json')
    def canonical(row):
        return [compact(v) for v in row]
    truth = {r[0]: canonical(r) for r in oracle}
    recovered = {r[0]: canonical(r) for r in table}
    matched = sum(recovered.get(n) == row for n, row in truth.items())
    ranges = []
    for p in parsed['pages']:
        ids = [int(r[0]) for r in poppler_rows(p['text'])]
        ranges.append([min(ids), max(ids)])
    evidence = {'expected_rows': oracle, 'recovered_rows': table,
                'oracle': 'Poppler 26.07.0 pdftotext -layout; PNGs pages 1-5 visually reviewed',
                'normalization': 'ignore whitespace only; retain character and row ordering'}
    write_json(RESULTS / 'pdf_quality_evidence.json', evidence)
    return {'expected_rows': len(oracle), 'recovered_rows': len(table), 'matched_rows': matched,
            'ordered': [r[0] for r in oracle] == [r[0] for r in table],
            'page_ranges': ranges, 'page_ranges_correct': ranges == expected['pdf']['page_row_ranges'],
            'scope': 'one born-digital 5-page table, not scanned/OCR/multi-column generalization'}


def evidence_audit():
    import lxml.html
    rows = []
    for tid in [*(f'A_detail_{i}' for i in range(1, 6)), 'B_detail_1', 'H_detail_1', 'D_company']:
        raw = (RAW / f'{tid}.raw.html').read_bytes()
        tree = lxml.html.fromstring(raw.decode('utf-8'))
        doc = read_json(RESULTS / 'replay' / f'{tid}.raw.json')
        for field, evidence in doc.get('_evidence', {}).items():
            nodes = tree.xpath(evidence['xpath']) if evidence else []
            quote = evidence.get('quote', '') if evidence else ''
            valid = bool(nodes) and quote in nodes[0].text_content()
            linked = bool(evidence) and sha256_hex(raw)[:12] in evidence['snapshot_id']
            rows.append({'target': tid, 'field': field, 'quote_resolves': valid,
                         'snapshot_hash_linked': linked})
    return {'checked': len(rows), 'valid': sum(r['quote_resolves'] and r['snapshot_hash_linked']
                                             for r in rows), 'fields': rows,
            'limitation': 'XPath + prefix quote, not byte offsets or full-field structured proof'}


def main():
    expected = read_json(HERE / 'fixtures/expected.json')
    out = {'details': detail_audit(expected), 'discovery': discovery_audit(),
           'pdf': pdf_audit(expected), 'field_evidence': evidence_audit()}
    write_json(RESULTS / 'quality.json', out)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
