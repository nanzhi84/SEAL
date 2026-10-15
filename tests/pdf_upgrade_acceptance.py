"""Real CLI/SQL/S3 PDF upgrade acceptance; no implementation imports.

Failure boundaries declared before execution:
- Each page's unique text or URI moves to a different page or disappears.
- An interior empty page shifts later page numbers or is dropped.
- Blank/corrupt PDF creates a successful Version or lacks explicit failure.
- Original bytes or archived result no longer match their SHA-256 references.
Run: uv run --no-sync python tests/pdf_upgrade_acceptance.py
"""
import io
import json
import threading
from http.server import ThreadingHTTPServer

import yaml
from lxml import html
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link

from dd492_pdf_acceptance import Acceptance, Handler, pdf, sha


def linked_pdf():
    reader = PdfReader(io.BytesIO(pdf(5)))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index == 2:
            writer.add_blank_page(width=612, height=792)
        else:
            writer.add_page(page)
            writer.add_annotation(index, Link(rect=(20, 690, 500, 720), url=f'https://example.org/pdf-page-{index + 1}'))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def execute(a):
    bodies = {'/linked.pdf': linked_pdf(), '/blank.pdf': pdf(3, blank=True),
              '/corrupt.pdf': b'%PDF-1.7\nnot a valid cross reference or page tree\n%%EOF'}
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.bodies, server.requests = bodies, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for label, body in bodies.items():
            name = label[1:].replace('.', '_')
            url = f'http://127.0.0.1:{server.server_port}{label}'
            config = {'id': 'dd-492', 'entry_urls': [url], 'allowed_hosts': ['127.0.0.1'],
                      'host_path_scopes': {'127.0.0.1': ['/']}, 'archive_approved': True,
                      'scope': 'Independent isolated PDF upgrade acceptance', 'robots': False,
                      'budget': {'requests': 5, 'seconds': 180, 'response_bytes': 10485760},
                      'crawl4ai': {'mode': 'http', 'max_depth': 0, 'word_count_threshold': 1}}
            source = a.work / (name + '.yaml')
            source.write_text(yaml.safe_dump(config))
            a.command(name, ['seal', 'collect', str(source), '--no-progress'])
            rows = [row for row in a.rows() if row['url'] == url]
            a.check(name + ': exactly one observation', len(rows) == 1)
            if not rows:
                continue
            row = rows[0]
            a.check(name + ': original bytes preserved', row['body_hash'] == sha(body) and a.object(row['body_hash']) == body)
            envelope = a.envelope(row)
            a.dump(name + '.observation.json', row)
            a.dump(name + '.result.json', envelope)
            if label != '/linked.pdf':
                a.check(name + ': explicit failure without Version', row['sequence'] is None and row['outcome'] == 'parse_failed' and bool(row['error']), outcome=row['outcome'], error=row['error'])
                if label == '/blank.pdf':
                    a.check('blank PDF identifies missing text layer', 'pdf_text_layer_required' in json.dumps({'error': row['error'], 'envelope': envelope}))
                continue
            result = envelope.get('result', {})
            dom = html.fromstring(result.get('cleaned_html') or '<div/>')
            nodes = dom.xpath('//*[@data-page]')
            a.check('all five pages remain in exact order including blank interior page', [n.get('data-page') for n in nodes] == ['1', '2', '3', '4', '5'])
            a.check('text PDF creates successful Version with page count five', row['sequence'] is not None and (result.get('metadata') or {}).get('pages') == 5)
            for page_number in range(1, 6):
                matches = [n for n in nodes if n.get('data-page') == str(page_number)]
                if not matches:
                    a.check(f'page {page_number} exists', False)
                    continue
                node = matches[0]
                expected = [] if page_number == 3 else [f'INDEPENDENT_PAGE_{page_number:03d}_COMPLETE_TEXT']
                import re
                actual = re.findall(r'INDEPENDENT_PAGE_\d+_COMPLETE_TEXT', node.text_content())
                a.check(f'page {page_number} contains only its own complete text', actual == expected, actual=actual)
                links = node.xpath('.//a/@href')
                expected_links = [] if page_number == 3 else [f'https://example.org/pdf-page-{page_number}']
                a.check(f'page {page_number} retains only its own URI annotation', links == expected_links, actual=links)
            extracted_links = result.get('links') or {}
            a.dump('linked_pdf.links.json', extracted_links)
        a.dump('fixture.requests.json', server.requests)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    acceptance = Acceptance()
    acceptance.dump('failure-boundaries.json', {'boundaries': __doc__, 'scope': 'Actual SEAL CLI with local deterministic PDF fixtures; fresh SQL database; original/result S3 hash verification'})
    try:
        execute(acceptance)
    except Exception as exc:
        acceptance.check('acceptance execution completes', False, error=type(exc).__name__ + ': ' + str(exc))
    failed = acceptance.finish()
    summary_path = acceptance.out / 'summary.json'
    summary = json.loads(summary_path.read_text())
    summary['scope'] = 'Isolated actual CLI/SQL/S3: 5-page text and URI identity with blank interior; entirely blank PDF; corrupt PDF; live source unverified'
    summary['prior_fixture_evidence'] = []
    summary_path.write_text(json.dumps(summary, indent=2) + '\n')
    raise SystemExit(failed)
