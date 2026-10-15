"""Independent CLI → SQL → S3 acceptance of native Crawl4AI 0.9.5 PDF output.

Declared failures before running: native title/paragraph or numeric-range cleanup
changes; pre wrappers reappear; pages beyond 2000 or empty interior pages vanish;
blank/corrupt originals lack archives or create Versions; old representation is
silently reused; repeated current contract creates duplicate Versions.
Run: uv run --no-sync python tests/pdf_native_contract_acceptance.py
Prerequisite: running local Postgres/S3 and private .env.local; no public sources.
"""
import argparse
import io
import json
import re
import subprocess
import tarfile
import threading
from http.server import ThreadingHTTPServer

import boto3
import yaml
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from crawl4ai.processors.pdf.processor import NaivePDFProcessorStrategy
from dd492_pdf_acceptance import ROOT, Acceptance, Handler, pdf, sha
from lxml import html
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def formatted_pdf():
    writer = PdfWriter()
    for index in range(5):
        page = writer.add_blank_page(width=612, height=792)
        if index == 2:
            continue
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        lines = ['Native PDF Acceptance Title', '', '1.1 Scope', '',
                 f'INDEPENDENT_PAGE_{index + 1:03d}_COMPLETE_TEXT',
                 'The numeric range is 10 - 20 and 30 - 40.', '',
                 'A second paragraph must remain readable.']
        stream = DecodedStreamObject()
        commands = ['BT /F1 12 Tf 25 720 Td 16 TL']
        for line in lines:
            commands.append('(' + line + ') Tj T*')
        commands.append('ET')
        stream.set_data('\n'.join(commands).encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def normalize_fragment(text):
    dom = html.fragment_fromstring(text or '', create_parent='div')
    return html.tostring(dom, encoding='unicode').strip()


def collect(a, source, label, old_path=None):
    original = a.env.get('PYTHONPATH')
    if old_path:
        a.env['PYTHONPATH'] = str(old_path)
    try:
        return a.command(label, ['python', '-m', 'seal.cli', 'collect', str(source), '--no-progress'])
    finally:
        if original is None:
            a.env.pop('PYTHONPATH', None)
        else:
            a.env['PYTHONPATH'] = original


def run(a):
    # Separate bucket, no production/shared object keys are written by this run.
    bucket = 'seal-native-acceptance-' + a.tag.lower().replace('_', '-')
    a.env['SEAL_S3_ACCESS_KEY'] = a.env['MINIO_ROOT_USER']
    a.env['SEAL_S3_SECRET_KEY'] = a.env['MINIO_ROOT_PASSWORD']
    a.s3 = boto3.client('s3', endpoint_url=a.env['SEAL_S3_ENDPOINT'],
                        aws_access_key_id=a.env['SEAL_S3_ACCESS_KEY'],
                        aws_secret_access_key=a.env['SEAL_S3_SECRET_KEY'])
    a.s3.create_bucket(Bucket=bucket)
    a.env['SEAL_S3_BUCKET'] = bucket
    a.dump('isolation.json', {'database': a.db, 'bucket': bucket, 'private_work': str(a.work)})
    old_dir = a.work / 'baseline'
    old_dir.mkdir()
    baseline = '26a31f2edaf6c1fd226df02142b6b26cf8d8cb68'
    archive = subprocess.run(['git', 'archive', baseline, 'src'], cwd=ROOT, check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(old_dir, filter='data')
    a.dump('baseline.json', {'commit': baseline, 'method': 'git archive pinned baseline src; subprocess PYTHONPATH, no runtime monkeypatch'})
    bodies = {'/formatted.pdf': formatted_pdf(), '/long.pdf': pdf(2001),
              '/blank.pdf': pdf(3, blank=True), '/corrupt.pdf': b'%PDF-1.7\nbroken\n%%EOF'}
    if OPTIONS.skip_long:
        bodies.pop('/long.pdf')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.bodies, server.requests = bodies, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for route, body in bodies.items():
            name = route[1:].replace('.', '_')
            path = a.work / route[1:]
            path.write_bytes(body)
            url = f'http://127.0.0.1:{server.server_port}{route}'
            config = {'id': 'dd-492', 'entry_urls': [url], 'allowed_hosts': ['127.0.0.1'],
                      'host_path_scopes': {'127.0.0.1': ['/']}, 'archive_approved': True,
                      'scope': 'Independent native PDF contract fixture', 'robots': False,
                      'budget': {'requests': 5, 'seconds': 600, 'response_bytes': 10485760},
                      'crawl4ai': {'mode': 'http', 'max_depth': 0, 'word_count_threshold': 1}}
            source = a.work / (name + '.yaml')
            source.write_text(yaml.safe_dump(config))
            old_versions = None
            if route == '/formatted.pdf':
                result = collect(a, source, name + '_baseline', old_dir / 'src')
                a.check('baseline CLI exits successfully', result.returncode == 0)
                old_row = [r for r in a.rows() if r['url'] == url][-1]
                old_envelope = a.envelope(old_row)
                a.dump('baseline-result.json', old_envelope)
                a.check('baseline is successful old pre representation', old_row['sequence'] == 1 and '<pre>' in old_envelope['result']['cleaned_html'])
                old_versions = a.versions()
            result = collect(a, source, name)
            a.check(name + ': CLI exit matches terminal outcome', (result.returncode != 0) if route in ('/blank.pdf', '/corrupt.pdf') else result.returncode == 0)
            rows = [r for r in a.rows() if r['url'] == url]
            a.check(name + ': expected observations', len(rows) == (2 if old_versions else 1))
            row = rows[-1]
            a.check(name + ': exact original archive', row['body_hash'] == sha(body) and row['archive_ref'] == sha(body) and a.object(sha(body)) == body)
            envelope = a.envelope(row)
            a.dump(name + '.result.json', envelope)
            a.dump(name + '.observation.json', row)
            if route in ('/blank.pdf', '/corrupt.pdf'):
                a.check(name + ': explicit parse failure without Version', row['outcome'] == 'parse_failed' and row['sequence'] is None and bool(row['error']))
                if route == '/blank.pdf':
                    a.check('blank PDF explicit text-layer diagnostic', 'pdf_text_layer_required' in json.dumps(envelope) + str(row['error']))
                continue
            result = envelope['result']
            dom = html.fromstring(result['cleaned_html'])
            nodes = dom.xpath('//*[@data-page]')
            expected_count = 2001 if route == '/long.pdf' else 5
            a.check(name + ': every page in exact order', [n.get('data-page') for n in nodes] == [str(i) for i in range(1, expected_count + 1)])
            a.check(name + ': no pre wrappers', not dom.xpath('//pre'))
            a.check(name + ': successful Version and page count', row['sequence'] is not None and result['metadata']['pages'] == expected_count)
            markers = re.findall(r'INDEPENDENT_PAGE_(\d+)_COMPLETE_TEXT', a.text(envelope))
            expected_markers = [f'{i:03d}' for i in range(1, expected_count + 1) if route == '/long.pdf' or i != 3]
            a.check(name + ': Markdown preserves full page sequence once', markers == expected_markers, count=len(markers))
            if route == '/formatted.pdf':
                native = NaivePDFProcessorStrategy(extract_images=False, max_pages=None).process(path)
                a.dump('native-sdk-pages.json', [{'page': p.page_number, 'html': p.html, 'raw': p.raw_text} for p in native.pages])
                actual_pages = [''.join(html.tostring(child, encoding='unicode') for child in node) for node in nodes]
                for i, (actual, expected) in enumerate(zip(actual_pages, native.pages, strict=True), 1):
                    a.check(f'page {i}: exact native HTML structure and text', normalize_fragment(actual) == normalize_fragment(expected.html))
                a.check('native headings and paragraphs survive', bool(dom.xpath('//h2')) and bool(dom.xpath('//p')))
                a.check('native numeric range cleanup reaches Markdown', '1020' in a.text(envelope) and '3040' in a.text(envelope) and '10 - 20' not in a.text(envelope))
                native_html = '<html><body>' + ''.join(f'<div class="pdf-page" data-page="{p.page_number}">{p.html}</div>' for p in native.pages) + '</body></html>'
                native_md = DefaultMarkdownGenerator().generate_markdown(native_html, base_url=url).raw_markdown
                a.dump('native-sdk-markdown.json', {'raw_markdown': native_md})
                a.check('full Markdown equals native HTML conversion', a.text(envelope).strip() == native_md.strip())
                current = a.versions()
                a.check('new representation appends exactly one Version', len(current) == len(old_versions) + 1 and row['sequence'] == 2)
                a.check('historical Version remains immutable', all(old in current for old in old_versions))
                before = a.versions()
                collect(a, source, name + '_repeat')
                repeat = [r for r in a.rows() if r['url'] == url][-1]
                a.check('same body and native contract creates no Version', repeat['outcome'] == 'unchanged' and a.versions() == before)
        a.dump('fixture-requests.json', server.requests)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-long', action='store_true', help='Skip costly >2000 page SDK boundary')
    OPTIONS = parser.parse_args()
    acceptance = Acceptance()
    acceptance.dump('failure-boundaries.json', {'declared_before_run': __doc__})
    try:
        run(acceptance)
    except Exception as exc:
        acceptance.check('acceptance completes', False, error=type(exc).__name__ + ': ' + str(exc))
    failed = acceptance.finish()
    summary = json.loads((acceptance.out / 'summary.json').read_text())
    summary.update(scope='Fresh local CLI/SQL/private S3 bucket; native SDK layout, old-to-current immutable Versions, repeat idempotency, blank/corrupt failure archive' + ('; 2001-page boundary skipped' if OPTIONS.skip_long else '; 2001 pages'), prior_fixture_evidence=[], command='uv run --no-sync python tests/pdf_native_contract_acceptance.py' + (' --skip-long' if OPTIONS.skip_long else ''), unverified=[*(['2001-page boundary skipped'] if OPTIONS.skip_long else []), 'Public-source recrawl', 'production or populated production DB upgrade', 'OCR and visual/layout fidelity beyond SDK native text conversion'])
    acceptance.dump('summary.json', summary)
    raise SystemExit(failed)
