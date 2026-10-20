"""Assert the frozen #33 acceptance artifacts and emit the concise final result."""
import hashlib
import json
from pathlib import Path

from issue33_blind_acceptance import ART, ROOT, load_env, object_bytes, query, setup


def main():
    controlled = json.loads((ART / 'controlled/assertions.json').read_text())
    assert len(controlled) >= 19 and all(c['passed'] for c in controlled)
    reports = {}
    acquisition_hash_checks = {}
    for phase in ['controlled', 'real']:
        report = json.loads((ART / phase / 'delivery-reconciliation.json').read_text())
        assert report['API_CAS_SHA256_equal'] and report['SQL_API_rows_equal']
        assert report['workbook_rows'] == report['api_rows'] == (6 if phase == 'controlled' else 5)
        assert report['moved_offline_verify']['status'] == 'verified'
        assert all(c['resolved'] for c in report['lineage_checks'])
        assert report['full_history_coverage'] == 'UNKNOWN'
        expected = {'dd-492': 2, 'dd-499': 2} if phase == 'controlled' else {'dd-492': 3, 'dd-499': 2}
        for source, count in expected.items():
            summary = report['source_summary'][source]
            assert summary['current_deliverable_documents'] == count
            assert summary['quality_passed_documents'] == count
            assert summary['full_history_denominator'] == 'UNKNOWN'
        env = setup(phase)
        acquired = query(env, 'SELECT archive_ref FROM seal.acquisition WHERE archive_ref IS NOT NULL')
        for item in acquired:
            object_bytes(env, item['archive_ref'])
        acquisition_hash_checks[phase] = len(acquired)
        reports[phase] = report

    real_sql = json.loads((ART / 'real/after-collect-sql.json').read_text())
    assert sum(a['http_method'] == 'POST' for a in real_sql['acquisitions']) >= 1
    assert len(real_sql['resources']) == len(real_sql['versions']) == 5
    ocr_documents = []
    for ref in reports['real']['references']:
        if ref['kind'] != 'result_ref':
            continue
        doc = json.loads((ART / 'real/cas' / (ref['sha256'] + '.json')).read_bytes())
        assert len(doc['content']['markdown']) > 20
        for d in doc['extraction']['deliverables']:
            if d.get('method') == 'ocr':
                assert d['ocr']['engine'] == 'tesseract' and d['ocr']['languages'] == 'vie'
                ocr_documents.append({'external_key': doc['identity']['external_key'],
                                      'pages': d['analysis']['page_count']})
    assert ocr_documents and max(d['pages'] for d in ocr_documents) >= 9
    assert 'page_limit_exceeded' in json.dumps(reports['real']['snapshot_coverage'])
    api = json.loads((ART / 'controlled/api-results.json').read_text())
    rows = api['dd-499']['items']
    assert len(rows) == 2 and all(not r['原始文件'] and not r['来源详情页'] for r in rows)
    assert all(r['文件哈希'] == r['markdown_hash'] for r in rows)

    start = json.loads((ART / 'frozen-source-hashes-start.json').read_text())
    end = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted((ROOT / 'src/seal').rglob('*'))
           if p.is_file() and '__pycache__' not in p.parts}
    (ART / 'frozen-source-hashes-end.json').write_text(json.dumps(end, indent=2))
    assert start == end, 'source_changed_during_acceptance'
    env = load_env()
    secrets = [v.encode() for k, v in env.items()
               if k in ['SEAL_DATABASE_URL', 'SEAL_S3_ACCESS_KEY', 'SEAL_S3_SECRET_KEY', 'PGPW']
               and len(v) >= 8]
    files = [p for p in ART.rglob('*') if p.is_file()]
    assert not any(secret in p.read_bytes() for p in files for secret in secrets)
    result = {
        'status': 'PASS', 'controlled_assertions': len(controlled),
        'real_documents': 5, 'real_delivery_rows': 5, 'controlled_delivery_rows': 6,
        'real_ocr_documents': ocr_documents, 'all_acquisition_CAS_hash_checks': acquisition_hash_checks,
        'source_tree_sha256': hashlib.sha256(json.dumps(end, sort_keys=True).encode()).hexdigest(),
        'source_unchanged_during_acceptance': True,
        'test_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted((ROOT / 'tests').glob('issue33_blind_*')) if p.is_file()},
        'artifact_secret_value_hits': 0,
        'real_collection_receipts': {s: json.loads((ART / 'real' / (s + '-collect.json')).read_text())['result']
                                     for s in ['dd-492', 'dd-499']},
        'commands': [
            'PYTHONPATH=/Users/yoryon/SEAL-pr3/src /Users/yoryon/SEAL/.venv/bin/python tests/issue33_blind_acceptance.py controlled --fresh',
            'PYTHONPATH=/Users/yoryon/SEAL-pr3/src /Users/yoryon/SEAL/.venv/bin/python tests/issue33_blind_acceptance.py real --fresh',
            'PYTHONPATH=/Users/yoryon/SEAL-pr3/src /Users/yoryon/SEAL/.venv/bin/python tests/issue33_blind_final.py'],
        'scope': 'Real: bounded Vietnam government 3 documents and Federal Register 2 rules; full-history denominator UNKNOWN. Controlled: multiple complete documents in one shared response; official Federal listing metadata is not full body.',
        'prerequisites': ['isolated PostgreSQL CREATE DATABASE and S3 CreateBucket using private work/dev/env.sh',
                          'shared Python 3.12 venv; tesseract 5.5.3; vie/eng tessdata_fast models',
                          'public source reachability; loopback allowed only for controlled fixtures'],
        'limitations': ['No full-history coverage, statutory correctness, or OCR character accuracy claim.',
                        'No live br-ocr-v1 remote service exercised.']
    }
    (ART / 'final-summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != 'real_collection_receipts'},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
