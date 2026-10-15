"""Independent CLI/SQL/S3 acceptance. Failure boundaries precede this test.
Run: uv run --no-sync python tests/dd492_pdf_acceptance.py [--fixtures-only]
Never imports or reads SEAL implementation. Uses a fresh database and private state.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
from importlib.metadata import version
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import urljoin

import boto3
import psycopg
import yaml
from botocore.config import Config
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pypdf import PdfReader, PdfWriter
from lxml import html as lxml_html
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

ROOT = Path(__file__).resolve().parents[1]


def sha(body):
    return hashlib.sha256(body).hexdigest()


def pdf(pages, blank=False):
    writer = PdfWriter()
    for number in range(1, pages + 1):
        page = writer.add_blank_page(width=612, height=792)
        if not blank:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 25 700 Td (INDEPENDENT_PAGE_{number:03d}_COMPLETE_TEXT) Tj ET'.encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def preserve_utf16_units(text):
    """Independent lossless encoding oracle, never guesses broken font symbols."""
    output, unpaired = [], []
    index = 0
    while index < len(text):
        unit = ord(text[index])
        if 0xD800 <= unit <= 0xDBFF and index + 1 < len(text) and 0xDC00 <= ord(text[index + 1]) <= 0xDFFF:
            output.append(chr(0x10000 + ((unit - 0xD800) << 10) + ord(text[index + 1]) - 0xDC00))
            index += 2
            continue
        if 0xD800 <= unit <= 0xDFFF:
            escaped = "\\u" + f"{unit:04x}"
            output.append(escaped)
            unpaired.append(escaped)
        else:
            output.append(text[index])
        index += 1
    return ''.join(output), unpaired


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        body = self.server.bodies.get(self.path, b'User-agent: *\nAllow: /\n')
        self.send_response(200)
        self.send_header('Content-Type', 'application/pdf' if self.path.endswith('.pdf') else 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Acceptance:
    def __init__(self, resume=None):
        self.tag = resume or time.strftime('%Y%m%d_%H%M%S')
        self.work = ROOT / 'work' / ('dd492_pdf_acceptance_' + self.tag)
        base_out = ROOT / 'artifacts' / 'dd-492-pdf-acceptance' / self.tag
        self.runner_out = base_out / 'runner'
        self.out = base_out / ('retry-' + time.strftime('%Y%m%d_%H%M%S')) if resume else base_out
        self.work.mkdir(parents=True, exist_ok=bool(resume))
        if self.out.exists():
            self.out = self.out.with_name(self.out.name + '-' + str(time.time_ns()))
        self.out.mkdir(parents=True)
        self.checks = []
        self.env = dict(os.environ)
        for line in (ROOT / '.env.local').read_text().splitlines():
            match = re.match(r'(?:export\s+)?([A-Z0-9_]+)=(.*)', line.strip())
            if match:
                self.env[match[1]] = match[2].strip().strip('\"\'')
        self.env.pop('SEAL_ARCHIVE', None)
        self.env.update(SEAL_WORK_DIR=str(self.work / 'sdk'), SEAL_ALLOW_LOOPBACK='1', NO_COLOR='1')
        original = conninfo_to_dict(self.env['SEAL_DATABASE_URL'])
        self.db = 'seal_dd492_acceptance_' + self.tag
        if not resume:
            with psycopg.connect(make_conninfo(**original), autocommit=True) as conn:
                conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(self.db)))
        original['dbname'] = self.db
        self.env['SEAL_DATABASE_URL'] = make_conninfo(**original)
        self.s3 = boto3.client('s3', endpoint_url=self.env['SEAL_S3_ENDPOINT'],
                               aws_access_key_id=self.env['SEAL_S3_ACCESS_KEY'],
                               aws_secret_access_key=self.env['SEAL_S3_SECRET_KEY'],
                               region_name=self.env.get('SEAL_S3_REGION', 'us-east-1'),
                               config=Config(s3={'addressing_style': 'path'}))
        if not resume:
            self.command('init', ['seal', 'init'])
        self.dump('environment.json', {'database': self.db, 'work': str(self.work),
                  'command': 'uv run --no-sync python tests/dd492_pdf_acceptance.py',
                  'credentials': 'Read privately from .env.local; never recorded',
                  'test_sha256': sha(Path(__file__).read_bytes()), 'runtime_versions': {'python': sys.version.split()[0], 'crawl4ai': version('crawl4ai'), 'pypdf': version('pypdf'), 'psycopg': version('psycopg')}, 'bounded': {'pages': 10, 'pdfs': 'all PDFs on selected pages'}})

    def dump(self, name, value):
        (self.out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')

    def check(self, name, ok, **evidence):
        row = {'name': name, 'status': 'PASS' if ok else 'FAIL', **evidence}
        self.checks.append(row)
        print(row['status'], name, flush=True)
        self.dump('assertions.json', self.checks)

    def command(self, name, args, timeout=900):
        cmd = ['uv', 'run', '--no-sync', *args]
        started = time.monotonic()
        result = subprocess.run(cmd, cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=timeout)
        text = result.stdout + '\n' + result.stderr
        for key in ('SEAL_S3_ACCESS_KEY', 'SEAL_S3_SECRET_KEY', 'SEAL_DATABASE_URL'):
            text = text.replace(self.env.get(key, 'NONEXISTENT_SECRET_VALUE'), '[REDACTED]')
        (self.out / (name + '.log')).write_text(text)
        self.dump(name + '.command.json', {'argv': cmd, 'exit': result.returncode, 'duration_seconds': round(time.monotonic() - started, 3)})
        return result

    def rows(self):
        with psycopg.connect(self.env['SEAL_DATABASE_URL'], row_factory=dict_row) as conn:
            return conn.execute('SELECT o.*, v.sequence, v.result_ref AS version_result_ref, v.body_hash AS version_body_hash FROM seal.observation o LEFT JOIN seal.version v ON v.id=o.version_id ORDER BY o.observed_at').fetchall()

    def object(self, digest):
        response = self.s3.get_object(Bucket=self.env['SEAL_S3_BUCKET'], Key=digest)
        try:
            body = response['Body'].read()
        finally:
            response['Body'].close()
        self.check('S3 object has exact content-addressed bytes', sha(body) == digest, digest=digest)
        return body

    def envelope(self, row):
        return json.loads(self.object(row['result_ref'])) if row.get('result_ref') else {}

    def text(self, envelope):
        result = envelope.get('result', envelope)
        markdown = result.get('markdown') or {}
        return markdown.get('raw_markdown', '') if isinstance(markdown, dict) else str(markdown)

    def fixtures(self):
        pages = getattr(self, 'long_pages', 2001)
        bodies = {'/long.pdf': pdf(pages)}
        if not getattr(self, 'long_only', False):
            bodies['/blank.pdf'] = pdf(3, blank=True)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.bodies, server.requests = bodies, []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for label, body in bodies.items():
                url = f'http://127.0.0.1:{server.server_port}{label}'
                config = {'id': 'dd-492', 'entry_urls': [url], 'allowed_hosts': ['127.0.0.1'],
                          'host_path_scopes': {'127.0.0.1': ['/']}, 'archive_approved': True,
                          'scope': 'Independent isolated PDF text-layer acceptance', 'robots': False,
                          'budget': {'requests': 5, 'seconds': 180, 'response_bytes': 10485760},
                          'crawl4ai': {'mode': 'http', 'max_depth': 0, 'word_count_threshold': 1}}
                name = label.strip('/').replace('.', '_')
                source = self.work / (name + '.yaml')
                source.write_text(yaml.safe_dump(config))
                self.command(name, ['seal', 'collect', str(source), '--no-progress'])
                rows = [row for row in self.rows() if row['url'] == url]
                self.check(name + ': exactly one observation', len(rows) == 1, count=len(rows))
                if not rows:
                    continue
                row = rows[0]
                self.check(name + ': original bytes preserved', row['body_hash'] == sha(body) and self.object(row['body_hash']) == body)
                envelope = self.envelope(row)
                self.dump(name + '.observation.json', row)
                self.dump(name + '.result.json', envelope)
                if name == 'long_pdf':
                    text = self.text(envelope)
                    missing = [i for i in range(1, pages + 1) if f'INDEPENDENT_PAGE_{i:03d}_COMPLETE_TEXT' not in text]
                    self.check(f'{pages}-page PDF retains every page including beyond SDK 2000-page default', row['sequence'] is not None and not missing, missing_pages=missing)
                    actual_pages = len(PdfReader(io.BytesIO(body)).pages)
                    metadata_pages = (envelope.get('result', {}).get('metadata') or {}).get('pages')
                    self.check('long PDF uses the lossless verbatim text contract', (envelope.get('result', {}).get('metadata') or {}).get('pdf_text_contract') == 'verbatim_v1')
                    self.check('long PDF actual original and SDK page counts match', actual_pages == pages and metadata_pages == actual_pages, actual_pages=actual_pages, metadata_pages=metadata_pages)
                    markers = re.findall(r'INDEPENDENT_PAGE_(\d+)_COMPLETE_TEXT', text)
                    self.check('long PDF text preserves page order exactly once', markers == [f'{i:03d}' for i in range(1, pages + 1)], marker_count=len(markers), expected_pages=pages)
                    old_versions = self.versions()
                    self.command('long_pdf_same_contract', ['seal', 'collect', str(source), '--no-progress'])
                    second = [entry for entry in self.rows() if entry['url'] == url][-1]
                    self.check('same original and verbatim contract is unchanged without new Version', second['outcome'] == 'unchanged' and self.versions() == old_versions, outcome=second['outcome'], version_count=len(self.versions()))
                else:
                    diagnostic = json.dumps({'error': row['error'], 'envelope': envelope}, default=str)
                    self.check('no text layer explicitly fails and creates no Version', row['sequence'] is None and row['outcome'] == 'parse_failed' and 'pdf_text_layer_required' in diagnostic, outcome=row['outcome'], error=row['error'])
            self.dump('fixture.requests.json', server.requests)
        finally:
            server.shutdown()
            server.server_close()

    def live(self):
        self.live_executed = True
        state = self.work / 'harvest'
        live_out = self.runner_out
        resume_source = str(live_out / 'dd-492.yaml') if getattr(self, 'audit_only', False) else 'sources/dd-492.yaml'
        args = ['python', '-m', 'seal.vietnam_documents', resume_source, '--state', str(state), '--artifacts', str(live_out), '--max-pages', '10']
        if not getattr(self, 'audit_only', False):
            self.command('live-first', args, timeout=7200)
        page_docs = []
        expected_pdfs = set()
        for number in range(1, 11):
            receipt = json.loads((live_out / f'page-{number:04d}.json').read_text())
            raw_bytes = Path(receipt['html_path']).read_bytes()
            raw = raw_bytes.decode('utf-8', errors='replace')
            self.check('list original HTML hash matches evidence', sha(raw_bytes) == receipt['html_hash'], page=number)
            links = re.findall(r'href=[\"\']([^\"\']+)[\"\']', raw, re.I)
            docs = {urljoin(receipt['request']['url'], html.unescape(link)) for link in links if 'docid=' in link.lower()}
            actual_docs = set(receipt['doc_urls'])
            self.check('list page doc URLs match original HTML', docs == actual_docs and bool(docs), page=number, html_doc_count=len(docs), recorded_doc_count=len(actual_docs))
            page_docs.append(docs)
            request = receipt['request']
            self.check('list page has actual request evidence', request['method'] == ('GET' if number == 1 else 'POST') and (number == 1 or (bool(request.get('event_target')) and request.get('event_argument') == f'Page${number}')), page=number, request=request)
            self.check('every listed document has detail evidence', {d['url'] for d in receipt['details']} == actual_docs, page=number)
            detail_pdfs = set()
            for detail in receipt['details']:
                original_bytes = Path(detail['html_path']).read_bytes()
                self.check('detail original HTML hash matches evidence', sha(original_bytes) == detail['html_hash'], url=detail['url'])
                original = original_bytes.decode('utf-8', errors='replace')
                links = re.findall(r'href=[\"\']([^\"\']+)[\"\']', original, re.I)
                detected = {urljoin(detail['url'], html.unescape(link)) for link in links if '.pdf' in link.lower()}
                recorded = set(detail['pdf_urls'])
                self.check('detail PDF links match original HTML', detected == recorded, url=detail['url'], original_links=sorted(detected), recorded_links=sorted(recorded))
                detail_pdfs.update(recorded)
            self.check('list PDF manifest covers all detail attachments', detail_pdfs == set(receipt['pdf_urls']), page=number)
            expected_pdfs.update(detail_pdfs)
        fingerprints = [sha('\n'.join(sorted(docs)).encode()) for docs in page_docs]
        self.check('ten historical pages have distinct nonempty content', len(set(fingerprints)) == 10 and all(page_docs), fingerprints=fingerprints)
        manifest = json.loads((live_out / 'pdf-manifest.json').read_text())
        self.check('all PDFs on ten pages are selected without a sample cap', {item['url'] for item in manifest} == expected_pdfs, expected_count=len(expected_pdfs), manifest_count=len(manifest))
        before = self.rows()
        latest = {row['url']: row for row in before if row['url'] in expected_pdfs}
        attempts = {url: sum(row['url'] == url for row in before) for url in expected_pdfs}
        self.dump('attempt-counts.json', attempts)
        successful = [row for row in latest.values() if row['sequence'] is not None]
        self.check('at least three real PDFs have successful Versions', len(successful) >= 3, count=len(successful))
        real_rows = list(latest.values())
        non_text_layer_failures = [row['url'] for row in real_rows if row['sequence'] is None and not (row['outcome'] == 'parse_failed' and 'pdf_text_layer_required' in str(row['error']))]
        self.check('all PDF attempts succeed or explicitly require a text layer', not non_text_layer_failures, non_text_layer_failures=non_text_layer_failures)
        self.check('every selected PDF has an observation', {row['url'] for row in real_rows} == expected_pdfs)
        for index, row in enumerate(real_rows):
            if not row['body_hash']:
                self.check('PDF without body explicitly fails', row['sequence'] is None and bool(row['error']), url=row['url'], outcome=row['outcome'], error=row['error'])
                continue
            self.check('PDF archive reference points to the exact original attachment', row['archive_ref'] == row['body_hash'] and row['body_hash'] in (row['attachment_refs'] or []), url=row['url'], archive_ref=row['archive_ref'])
            body = self.object(row['body_hash'])
            materialized = self.work / 'pdf-audit' / (row['body_hash'] + '.pdf')
            materialized.parent.mkdir(exist_ok=True)
            if not materialized.exists():
                materialized.write_bytes(body)
            # Frozen collector evidence is the acquisition receipt; never redownload
            # all originals merely to verify already content-addressed S3 objects.
            acquisition = [entry for entry in before if entry['url'] == row['url'] and entry.get('body_hash') == row['body_hash']]
            self.check('archived original agrees with frozen acquisition observations', bool(acquisition), url=row['url'], digest=row['body_hash'], acquisition_ids=[str(entry['id']) for entry in acquisition])
            try:
                reader = PdfReader(io.BytesIO(body))
            except Exception as exc:
                self.check('invalid PDF content is an explicit failure without Version', row['sequence'] is None and bool(row['error']), url=row['url'], parser_error=type(exc).__name__, outcome=row['outcome'])
                self.dump(f'real-pdf-{index}.observation.json', row)
                continue
            envelope = self.envelope(row)
            text = self.text(envelope)
            actual = re.sub(r'\s+', '', text)
            dom = lxml_html.fromstring(envelope.get('result', {}).get('cleaned_html') or '<div/>')
            page_nodes = dom.xpath('//*[@data-page]')
            page_map = {int(node.attrib['data-page']): node for node in page_nodes}
            if row['sequence'] is not None:
                sequence = [int(node.attrib['data-page']) for node in page_nodes]
                self.check('SDK has every original page in order', sequence == list(range(1, len(reader.pages) + 1)), url=row['url'], native_page_nodes=len(page_nodes), original_pages=len(reader.pages))
                self.check('current successful PDF declares verbatim contract', (envelope.get('result', {}).get('metadata') or {}).get('pdf_text_contract') == 'verbatim_v1', url=row['url'])
            missing, nonempty, unpaired_units = [], 0, []
            for page_number, page in enumerate(reader.pages, 1):
                try:
                    native_text, units = preserve_utf16_units(page.extract_text() or '')
                    unpaired_units.extend(units)
                    independent = re.sub(r'\s+', '', native_text)
                except Exception:
                    missing.append(page_number)
                    continue
                if independent:
                    nonempty += 1
                page_text = re.sub(r'\s+', '', page_map[page_number].text_content()) if page_number in page_map else ''
                if row['sequence'] is not None and independent != page_text:
                    missing.append(page_number)
            metadata_pages = (envelope.get('result', {}).get('metadata') or {}).get('pages')
            if row['body_hash'] == '957aed910051536ad4747a2eb36c33e408c8a9fe8dff264c54a91a8439e5f0b0' and row['sequence'] is not None:
                encoding = (envelope.get('result', {}).get('metadata') or {}).get('pdf_text_encoding') or {}
                valid_utf8 = True
                try:
                    json.dumps(envelope, ensure_ascii=False).encode('utf-8', errors='strict')
                except UnicodeEncodeError:
                    valid_utf8 = False
                self.check('1118-page repaired result remains valid JSON and UTF8', valid_utf8 and metadata_pages == 1118, metadata_pages=metadata_pages)
                self.check('invalid original UTF16 units remain reversibly escaped with explicit warning', len(unpaired_units) == 80 and encoding.get('unpaired_code_units') == 80 and encoding.get('representation') == 'literal_utf16_escape' and all(text.count(unit) >= unpaired_units.count(unit) for unit in set(unpaired_units)), independent_unpaired_units=len(unpaired_units), encoding_warning=encoding, semantic_recovery='Unknown mathematical symbols are not guessed')
            if row['sequence'] is not None:
                self.check('Version references the same original and SDK result as Observation', (row['outcome'] == 'unchanged' or row['version_result_ref'] == row['result_ref']) and row['version_body_hash'] == row['body_hash'], url=row['url'])
                self.check('real PDF every text page is represented', nonempty > 0 and not missing and metadata_pages == len(reader.pages), url=row['url'], pages=len(reader.pages), metadata_pages=metadata_pages, nonempty_pages=nonempty, missing_pages=missing, sha256=row['body_hash'])
            else:
                self.check('failed PDF is explicit and never has a Version', bool(row['error']) and row['outcome'] != 'parsed', url=row['url'], outcome=row['outcome'], error=row['error'])
                if not nonempty:
                    diagnostic = json.dumps({'error': row['error'], 'envelope': envelope}, default=str)
                    self.check('real no-text PDF explicitly requires a text layer', row['outcome'] == 'parse_failed' and 'pdf_text_layer_required' in diagnostic, url=row['url'])
            self.dump(f'real-pdf-{index}.observation.json', row)
            self.dump(f'real-pdf-{index}.text-evidence.json', {'pages': len(reader.pages), 'metadata_pages': metadata_pages, 'nonempty_pages': nonempty, 'missing_pages': missing, 'sha256': row['body_hash'], 'comparison': 'Every substantive page exact whitespace-normalized original text, including all numbers/punctuation; only reversible UTF16 representation conversion permitted'})
        self.command('live-resume', args, timeout=7200)
        after = self.rows()
        first_ids = {str(row['id']) for row in before}
        duplicates = [row['url'] for row in after if row['url'] in expected_pdfs and str(row['id']) not in first_ids]
        self.check('resume does not collect terminal PDF URLs again', not duplicates, duplicate_urls=duplicates)
        summary = json.loads((live_out / 'summary.json').read_text())
        self.check('runner selected-page completion agrees with observations', summary['selected_pages_complete'] and summary['counts'].get('pending', 0) == 0 and summary['counts'].get('parsed', 0) + summary['counts'].get('failed', 0) == len(expected_pdfs) and summary['pdfs'] == len(expected_pdfs), summary=summary)
        self.dump('live.observations.json', real_rows)

    def retry_network(self, parser_fix=False, reparse_fix=False):
        self.network_retry_executed = True
        before = self.rows()
        latest = {row['url']: row for row in before}
        allowed = {url for url, row in latest.items() if (row['error'] == 'pdf_page_parse_failed' if parser_fix else row['outcome'] == 'network_failed')}
        if reparse_fix:
            original_path = sorted(self.runner_out.parent.glob('retry-*/reparse.original-observations.json'))[-1]
            original = {row['url']: row for row in json.loads(original_path.read_text())}
            original_success = {url for url, row in original.items() if row['outcome'] in ('parsed', 'unchanged')}
            allowed &= original_success
        flag = '--retry-reparse-network' if reparse_fix else '--retry-parser-fix' if parser_fix else '--retry-network'
        args = ['python', '-m', 'seal.vietnam_documents', 'sources/dd-492.yaml',
                '--state', str(self.work / 'harvest'), '--artifacts', str(self.runner_out),
                '--max-pages', '10', flag]
        self.command('reparse-network-retry' if reparse_fix else 'parser-fix-retry' if parser_fix else 'network-retry', args, timeout=7200)
        old_ids = {str(row['id']) for row in before}
        new_rows = [row for row in self.rows() if str(row['id']) not in old_ids]
        self.check('explicit retry collects only the declared previous failure subset', all(row['url'] in allowed for row in new_rows), allowed_count=len(allowed), retried_count=len(new_rows), unexpected_urls=[row['url'] for row in new_rows if row['url'] not in allowed])
        self.dump('network-retry.observations.json', new_rows)
        self.dump('network-retry.summary.json', json.loads((self.runner_out / 'summary.json').read_text()))

    def versions(self):
        with psycopg.connect(self.env['SEAL_DATABASE_URL'], row_factory=dict_row) as conn:
            return conn.execute('SELECT * FROM seal.version ORDER BY cursor').fetchall()

    def reparse_successful(self):
        self.network_retry_executed = True
        before = self.rows()
        latest = {row['url']: row for row in before}
        eligible = {url for url, row in latest.items() if row['outcome'] in ('parsed', 'unchanged')}
        old_versions = self.versions()
        self.dump('reparse.original-versions.json', old_versions)
        self.dump('reparse.original-observations.json', before)
        args = ['python', '-m', 'seal.vietnam_documents', 'sources/dd-492.yaml',
                '--state', str(self.work / 'harvest'), '--artifacts', str(self.runner_out),
                '--max-pages', '10', '--reparse-successful']
        self.command('reparse-successful', args, timeout=10800)
        after = self.rows()
        old_ids = {str(row['id']) for row in before}
        new_rows = [row for row in after if str(row['id']) not in old_ids]
        self.check('corrective reparse only observes the original successful subset', all(row['url'] in eligible for row in new_rows), eligible_count=len(eligible), new_count=len(new_rows), unexpected=[row['url'] for row in new_rows if row['url'] not in eligible])
        current_versions = {str(row['id']): row for row in self.versions()}
        changed = [str(row['id']) for row in old_versions if current_versions.get(str(row['id'])) != row]
        self.check('all historical immutable Versions remain exactly unchanged', not changed, changed_version_ids=changed)
        self.dump('reparse.new-observations.json', new_rows)
        self.dump('reparse.latest-versions.json', list(current_versions.values()))
        self.dump('reparse.summary.json', json.loads((self.runner_out / 'summary.json').read_text()))

    def finish(self):
        failures = [row for row in self.checks if row['status'] == 'FAIL']
        summary = {'status': 'FAIL' if failures else 'PASS', 'checks': len(self.checks), 'failures': failures,
                   'artifact': str(self.out), 'database': self.db,
                   'scope': 'Finite declared failure subset retry; complete PDF audit pending' if getattr(self, 'network_retry_executed', False) else 'Ten real ASP.NET pages and every linked PDF original/text/terminal outcome/resume; full historical completion unverified' if getattr(self, 'live_executed', False) else 'Isolated long-page and optionally no-text PDF fixtures only; live source unverified',
                   'prior_fixture_evidence': [str(ROOT / 'artifacts/dd-492-pdf-acceptance/20261014_174232'), str(ROOT / 'artifacts/dd-492-pdf-acceptance/20261014_155951')]}
        self.dump('summary.json', summary)
        print(json.dumps(summary, ensure_ascii=False))
        return bool(failures)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixtures-only', action='store_true')
    parser.add_argument('--long-pages', type=int, default=2001)
    parser.add_argument('--long-only', action='store_true', help='Only test long PDF boundary; prior no-text evidence retained')
    parser.add_argument('--skip-fixtures', action='store_true', help='Prior fixture evidence already passed; run only fresh live acceptance')
    parser.add_argument('--retry-reparse-network', action='store_true', help='One finite retry of corrective-network failures only')
    parser.add_argument('--reparse-successful', action='store_true', help='Correct the existing successful subset without touching other URLs')
    parser.add_argument('--retry-parser-fix', action='store_true', help='One bounded known parser-failure subset retry')
    parser.add_argument('--retry-network', action='store_true', help='Execute one finite network-only retry without full audit')
    parser.add_argument('--audit-existing', action='store_true', help='Audit existing runner outputs before an ordinary resume dedup check')
    parser.add_argument('--resume', help='Existing isolated acceptance timestamp; preserves original receipts')
    options = parser.parse_args()
    acceptance = Acceptance(options.resume)
    acceptance.audit_only = options.audit_existing
    acceptance.long_pages = options.long_pages
    acceptance.long_only = options.long_only
    try:
        if options.reparse_successful:
            acceptance.reparse_successful()
        elif options.retry_network or options.retry_parser_fix or options.retry_reparse_network:
            acceptance.retry_network(parser_fix=options.retry_parser_fix, reparse_fix=options.retry_reparse_network)
        else:
            if not options.resume and not options.skip_fixtures:
                acceptance.fixtures()
            if not options.fixtures_only:
                acceptance.live()
    except Exception as exc:
        acceptance.check('acceptance execution completes', False, error=type(exc).__name__ + ': ' + str(exc))
    sys.exit(acceptance.finish())
