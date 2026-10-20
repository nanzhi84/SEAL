"""API-only consumption checks: 16 business fields, exact bytes and archived lineage.

Preconditions are declared in issue33_blind_boundaries.json. The caller supplies storage
and OCR configuration; this module never selects a personal environment file.
"""
import hashlib
import json
import os
import socket
import subprocess
import time
from datetime import timezone
from pathlib import Path
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet

from issue33_blind_acceptance import ART, PYTHON, ROOT, WORK, cli, object_bytes, query

# This is the external business contract, independently stated by the acceptance test.
FIELDS = (
    ('资料名称', 'title'), ('解析后文件', 'markdown_file'), ('资料类型', 'document_type'),
    ('文件哈希', 'file_hash'), ('采集时间', 'acquired_at'), ('原始文件', 'original_file'),
    ('文件顺序', 'volume_order'), ('文号/案号', 'document_number'), ('发布机构', 'issuer'),
    ('发布日期', 'published_at'), ('生效日期', 'effective_at'), ('失效日期', 'expired_at'),
    ('状态', 'status'), ('来源名称', 'source_name'), ('来源详情页', 'detail_url'),
    ('来源记录号', 'source_record_hash'),
)
COLUMNS = tuple(name for name, _ in FIELDS)


def source_hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / 'src/seal').rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts}


def sql_value(row, field):
    value = row[field]
    if value is None:
        return ''
    if field == 'volume_order':
        return str(value)
    if field == 'acquired_at':
        return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    if field in {'published_at', 'effective_at', 'expired_at'}:
        return value.isoformat()
    return value


def lineage(env, phase):
    references, checks = [], []
    for version in query(env, 'SELECT id,result_ref,inputs_ref FROM seal.version ORDER BY id'):
        for kind in ['result_ref', 'inputs_ref']:
            body = object_bytes(env, version[kind])
            obj = json.loads(body)
            dest = ART / phase / 'cas' / f'{version[kind]}.json'
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(body)
            references.append({'version_id': version['id'], 'kind': kind,
                               'sha256': version[kind], 'schema': obj.get('schema')})
            if kind != 'inputs_ref':
                continue
            for item in obj['inputs']:
                original = object_bytes(env, item['archive_ref'])
                locator = item['locator']
                if 'json_pointer' in locator:
                    leaf = json.loads(original)
                    tokens = locator['json_pointer'].strip('/').split('/') if locator['json_pointer'] else []
                    for token in tokens:
                        token = token.replace('~1', '/').replace('~0', '~')
                        leaf = leaf[int(token)] if isinstance(leaf, list) else leaf[token]
                    assert leaf['document_number'] == obj['document']['external_key']
                if 'xpath' in locator:
                    from lxml import etree
                    assert etree.fromstring(original).xpath(locator['xpath'])
                checks.append({'version_id': version['id'], 'archive_ref': item['archive_ref'],
                               'locator': locator, 'resolved': True})
    return references, checks


def source_summary(env):
    reports = {}
    for source in ['dd-492', 'dd-499']:
        rows = query(env, 'SELECT r.external_key,v.result_ref FROM seal.resource r '
                     'JOIN seal.frontier f ON f.id=r.frontier_id '
                     'JOIN seal.version v ON v.id=r.current_version_id WHERE f.source_id=%s', (source,))
        docs = [json.loads(object_bytes(env, row['result_ref'])) for row in rows]
        reports[source] = {
            'current_consumable_documents': len(docs),
            'api_rows': sum(len(doc['deliverables']) for doc in docs),
            'original_acquired_documents': sum(any(d['original_file_ref'] for d in doc['deliverables']) for doc in docs),
            'quality_passed_documents': sum(all(d['quality_report'].get('recommended_route') in
                 ['accept', 'accept_ocr', 'accept_with_warning'] for d in doc['deliverables']) for doc in docs),
            'full_history_denominator': 'UNKNOWN',
            'extraction_methods': sorted({str(d.get('method') or d.get('extractor') or d.get('kind'))
                 for doc in docs for d in doc['extraction']['deliverables']}),
        }
    return reports


def consume(env, phase):
    snapshot, rc = cli(env, ['snapshot', 'create'], 'snapshot-create', phase)
    assert rc == 0, snapshot
    snapshot_id = snapshot['snapshot_id']
    _, rc = cli(env, ['snapshot', 'publish', snapshot_id], 'snapshot-publish', phase)
    assert rc == 0
    _, rc = cli(env, ['snapshot', 'inspect', snapshot_id, '--limit', '100'], 'snapshot-inspect', phase)
    assert rc == 0
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    token = os.urandom(24).hex()
    config = {'public_base_url': f'http://127.0.0.1:{port}',
              'token_key': Fernet.generate_key().decode(),
              'callers': [{'id': 'accept33', 'key_sha256': hashlib.sha256(token.encode()).hexdigest(),
                           'sources': ['dd-492', 'dd-499']}], 'workers': 2}
    path = WORK / phase / 'api-private.json'
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    process = subprocess.Popen([str(PYTHON), '-m', 'seal', 'serve', '--config', str(path),
                                '--port', str(port)], cwd=ROOT, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results, api_files, api_bodies = {}, {}, {}

    def request(url):
        req = Request(f'http://127.0.0.1:{port}' + url,
                      headers={'Authorization': 'Bearer ' + token})
        with urlopen(req, timeout=30) as response:
            return response.read(), dict(response.headers)

    try:
        ready = False
        for _ in range(60):
            if process.poll() is not None:
                break
            try:
                request('/healthz')
                ready = True
                break
            except OSError:
                time.sleep(0.1)
        assert ready, 'API_server_did_not_start'
        for source in ['dd-492', 'dd-499']:
            raw, _ = request('/results?source_id=' + source + '&limit=100')
            page = json.loads(raw)
            results[source] = page
            assert page['snapshot_id'] == snapshot_id and page['next_cursor'] is None
            for row in page['items']:
                assert tuple(row)[:16] == COLUMNS
                assert len(row['来源记录号']) == 64
                for key in ['文件哈希', 'markdown_hash']:
                    sha256 = row[key]
                    if sha256 not in api_files:
                        body, headers = request('/files/' + sha256)
                        assert hashlib.sha256(body).hexdigest() == sha256
                        assert body == object_bytes(env, sha256)
                        api_bodies[sha256] = body
                        api_files[sha256] = {'bytes': len(body), 'content_type': headers.get('Content-Type'),
                                             'sha256': sha256}
                    if key == 'markdown_hash':
                        assert api_bodies[sha256].decode('utf-8').strip()

    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=20)
    (ART / phase / 'api-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    (ART / phase / 'api-files.json').write_text(json.dumps(api_files, ensure_ascii=False, indent=2))
    api_rows = [row for page in results.values() for row in page['items']]
    rows = query(env, 'SELECT d.*,r.id AS resource_id FROM seal.delivery_row d '
                 'JOIN seal.version v ON v.id=d.version_id JOIN seal.resource r ON r.id=v.resource_id '
                 'JOIN seal.snapshot_item si ON si.version_id=v.id WHERE si.snapshot_id=%s '
                 'ORDER BY d.version_id,d.row_number', (snapshot_id,))
    assert len(rows) == len(api_rows) == (5 if phase == 'real' else 6)
    for row in rows:
        matches = [item for item in api_rows if item['version_id'] == row['version_id']
                   and item['row_number'] == row['row_number']]
        assert len(matches) == 1
        item = matches[0]
        assert item['resource_id'] == row['resource_id'] and item['markdown_hash'] == row['markdown_hash']
        for label, field in FIELDS:
            assert item[label] == sql_value(row, field), ('SQL_API_field', label, item, row)
    references, checks = lineage(env, phase)
    hashes = source_hashes()
    (ART / phase / 'source-hashes.json').write_text(json.dumps(hashes, indent=2))
    coverage = query(env, 'SELECT source_id,coverage FROM seal.snapshot_source WHERE snapshot_id=%s '
                     'ORDER BY source_id', (snapshot_id,))
    report = {'contract': 'API-only', 'snapshot_id': snapshot_id, 'business_columns': COLUMNS,
              'API_CAS_SHA256_equal': True, 'SQL_API_all_16_fields_equal': True,
              'api_rows': len(api_rows), 'api_file_hashes': sorted(api_files),
              'snapshot_coverage': coverage, 'references': references, 'lineage_checks': checks,
              'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'test_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'full_history_coverage': 'UNKNOWN', 'source_summary': source_summary(env),
              'source_tree_sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}
    (ART / phase / 'api-reconciliation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'phase': phase, 'rows': len(api_rows), 'API_CAS_SQL': 'PASS'}), flush=True)
    return report
