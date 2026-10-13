"""Live bounded MOE 2021 notice XLS acceptance; no implementation/config mutation.
Failures: wrong seed, missing XLS links, missing archive/lineage/cells/export,
corrupt hashes, missing recheck observations, unexpected stable-input versions.
Requires local PostgreSQL createdb, uv frozen runtime, live MOE access.
Run: uv run --frozen python tests/dd468_playground_e2e.py [unique-run-name]
"""
import csv
csv.field_size_limit(64 * 1024 * 1024)
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import psycopg
from psycopg.rows import dict_row
from lxml import html
import xlrd
import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTRY = 'http://www.moe.gov.cn/jyb_xxgk/s5743/s5744/A03/202110/t20211025_574874.html'
run = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime('moe-e2e-%Y%m%d-%H%M%S')
assert all(c.isalnum() or c in '-_' for c in run)
db = 'seal_' + run.replace('-', '_')
art = ROOT / 'playgroud/dd-468/artifacts' / run
work = ROOT / 'playgroud/dd-468/work' / run
art.mkdir(parents=True, exist_ok=False)
work.mkdir(parents=True, exist_ok=False)
os.chmod(art, 0o700)
os.chmod(work, 0o700)
env = dict(os.environ, SEAL_DATABASE_URL='postgresql:///' + db, SEAL_ARCHIVE=str(work / 'archive'))
config = ROOT / 'playgroud/dd-468/dd-468.yaml'
shutil.copyfile(config, art / 'dd-468.yaml')
(art / 'environment.json').write_text(json.dumps({
    'command': 'uv run --frozen python tests/dd468_playground_e2e.py ' + run,
    'cwd': str(ROOT), 'database': db, 'archive': env['SEAL_ARCHIVE'],
    'python': sys.version, 'prerequisites': 'Local PostgreSQL with createdb permission; uv frozen dependencies; public MOE network access',
    'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
    'git_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True)),
}, ensure_ascii=False, indent=2))
checks = []
def check(name, value, detail=None):
    checks.append(dict(assertion=name, passed=bool(value), detail=detail))
    if not value:
        raise AssertionError(name)
def command(name, args):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)
    (art / (name + '.stdout')).write_text(result.stdout)
    (art / (name + '.stderr')).write_text(result.stderr)
    check(name + ' exit', result.returncode == 0)
    return result.stdout
def obj(key):
    data = (work / 'archive/objects' / key[:2] / key[2:]).read_bytes()
    check('archive SHA256 ' + key, hashlib.sha256(data).hexdigest() == key)
    return data
try:
    cfg = yaml.safe_load(config.read_text())
    check('unique column seed', cfg['id'] == 'dd-468' and cfg['entry_urls'] == [ENTRY])
    command('createdb', ['createdb', db])
    command('init', ['uv', 'run', '--frozen', 'seal', 'init'])
    collect = json.loads(command('collect', ['uv', 'run', '--frozen', 'seal', 'collect', str(config)]))
    check('collect complete', collect['status'] == 'complete', collect.get('errors'))
    with psycopg.connect(env['SEAL_DATABASE_URL'], row_factory=dict_row) as con:
        def query(sql, args=()):
            return con.execute(sql, args).fetchall()
        observations = query('SELECT * FROM seal.fetch_observation WHERE run_id=%s', (collect['run_id'],))
        columns = [o for o in observations if o['url'] == ENTRY and o['status'] == 200]
        check('column archived', bool(columns))
        column = columns[-1]
        snapshot = json.loads(obj(column['snapshot_id']))
        raw = obj(snapshot['body_hash'])
        page = html.fromstring(raw)
        # All XLS anchors in the archived public page, independent of recipe CSS.
        expected = {urljoin(ENTRY, a) for a in page.xpath('//a/@href') if urlsplit(urljoin(ENTRY, a)).path.lower().endswith('.xls')}
        (art / 'column-public-xlss.json').write_text(json.dumps(sorted(expected), ensure_ascii=False, indent=2))
        check('public XLS set has XLS links', len(expected) > 0, sorted(expected))
        docs = query('SELECT d.*,v.id AS version_id,v.content,v.content_hash,v.body_hash,v.snapshot_id,v.observation_id,v.source_format FROM seal.document d JOIN seal.document_version v ON v.id=d.latest_document_version WHERE d.source_id=%s', ('dd-468',))
        xlss = [d for d in docs if d['source_format'] == 'xls']
        check('exact public XLS coverage', {d['url'] for d in xlss} == expected, {'expected': sorted(expected), 'actual': sorted(d['url'] for d in xlss)})
        evidence = []
        for d in xlss:
            body = obj(d['body_hash'])
            book = xlrd.open_workbook(file_contents=body)
            tables = [n for n in d['content']['children'] if n['type'] == 'table']
            check('all workbook sheets ' + d['url'], [t['name'] for t in tables] == book.sheet_names())
            cell_count = 0
            for sheet, table in zip(book.sheets(), tables):
                check('sheet dimensions ' + sheet.name, len(table['rows']) == sheet.nrows and all(len(row) == sheet.ncols for row in table['rows']))
                for row in range(sheet.nrows):
                    for col in range(sheet.ncols):
                        cell = sheet.cell(row, col)
                        value = cell.value
                        if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK): value = None
                        elif cell.ctype == xlrd.XL_CELL_BOOLEAN: value = bool(value)
                        elif cell.ctype == xlrd.XL_CELL_NUMBER and value.is_integer(): value = int(value)
                        elif cell.ctype == xlrd.XL_CELL_DATE: value = xlrd.xldate_as_datetime(value, book.datemode).isoformat()
                        if table['rows'][row][col] != value:
                            check('source cell equality', False, dict(sheet=sheet.name,row=row,col=col,expected=value,actual=table['rows'][row][col]))
                        cell_count += 1
            check('all source cells equal ' + d['url'], cell_count > 0, cell_count)
            canonical = json.dumps(d['content'], sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
            check('content hash ' + d['url'], hashlib.sha256(canonical).hexdigest() == d['content_hash'])
            lineage = query('SELECT * FROM seal.discovery WHERE run_id=%s AND url=%s AND parent_url=%s AND parent_snapshot_id=%s AND parent_observation_id=%s', (collect['run_id'], d['url'], ENTRY, column['snapshot_id'], column['id']))
            check('column discovery evidence ' + d['url'], bool(lineage))
            linked = query('SELECT v.id FROM seal.document_version v JOIN seal.revision r ON r.id=v.revision_id JOIN seal.fetch_observation o ON o.id=v.observation_id WHERE v.id=%s AND v.body_hash=r.body_hash AND v.body_hash=o.body_hash AND v.snapshot_id=o.snapshot_id AND o.run_id=%s', (d['version_id'], collect['run_id']))
            check('version revision observation linked ' + d['url'], bool(linked))
            evidence.append(dict(url=d['url'], sheets=book.nsheets, cells=cell_count, body_hash=d['body_hash'], version_id=d['version_id'], observation_id=d['observation_id'], discovery=lineage))
        (art / 'xls-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        before = {d['url']: d for d in xlss}
    queued = json.loads(command('recheck-enqueue', ['uv', 'run', '--frozen', 'seal', 'recheck', str(config), '--enqueue']))
    command('worker', ['uv', 'run', '--frozen', 'seal', 'worker', '--once'])
    with psycopg.connect(env['SEAL_DATABASE_URL'], row_factory=dict_row) as con:
        state = con.execute('SELECT * FROM seal.run WHERE id=%s', (queued['run_id'],)).fetchone()
        check('recheck complete', state['status'] == 'complete', state['errors'])
        emissions = con.execute('SELECT e.*, d.url, v.body_hash FROM seal.document_emission e JOIN seal.document d ON d.id=e.document_id JOIN seal.document_version v ON v.id=e.document_version_id WHERE e.run_id=%s', (queued['run_id'],)).fetchall()
        check('recheck XLS coverage', expected <= {e['url'] for e in emissions})
        for e in emissions:
            if e['url'] in before and e['body_hash'] == before[e['url']]['body_hash']:
                check('stable input unchanged version ' + e['url'], e['document_version_id'] == before[e['url']]['version_id'] and e['change_kind'] == 'unchanged')
            obs = con.execute('SELECT * FROM seal.fetch_observation WHERE id=%s', (e['observation_id'],)).fetchone()
            check('fresh recheck observation', obs['run_id'] == queued['run_id'] and obs['origin'] == 'network')
            obj(obs['snapshot_id'])
            obj(obs['body_hash'])
        (art / 'recheck-evidence.json').write_text(json.dumps(emissions, ensure_ascii=False, indent=2, default=str))
        export = art / 'dd-468-documents.csv'
        with export.open('wb') as file, con.cursor().copy("COPY (SELECT d.id,d.url,v.id AS version_id,v.content FROM seal.document d JOIN seal.document_version v ON v.id=d.latest_document_version WHERE d.source_id='dd-468' ORDER BY d.url) TO STDOUT CSV HEADER") as copy:
            for data in copy:
                file.write(data)
        rows = list(csv.DictReader(export.open()))
        check('export complete XLSs with content', expected <= {r['url'] for r in rows} and all(json.loads(r['content'])['children'] for r in rows))
    status = 'PASS'
except Exception as error:
    status = 'FAIL'
    checks.append(dict(assertion='execution', passed=False, detail=repr(error)))
finally:
    (art / 'acceptance.json').write_text(json.dumps(dict(status=status, database=db, archive=str(work / 'archive'), scope='Only the archived 2021 notice public XLS set; whole-site completeness UNKNOWN', assertions=checks), ensure_ascii=False, indent=2))
    print(json.dumps(dict(status=status, artifact=str(art), assertions=len(checks)), ensure_ascii=False))
    sys.exit(0 if status == 'PASS' else 1)
