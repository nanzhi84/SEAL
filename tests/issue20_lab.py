"""Isolated services and receipts for Issue 20 business acceptance; no product mocks."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import boto3
import psycopg
import yaml
from botocore.config import Config
from psycopg.rows import dict_row

from issue20_fixture import Origin

ROOT = Path(__file__).resolve().parents[1]
MINIO = 'seal-minio:RELEASE.2025-10-15T17-29-55Z'
MC = 'minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n')


class Lab:
    def __init__(self):
        self.name = 'issue20-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        self.work, self.artifacts = (ROOT / x / self.name for x in ('work', 'artifacts'))
        self.work.mkdir(parents=True); self.artifacts.mkdir(parents=True)
        self.containers, self.checks, self.commands = [], [], []
        self.secrets = [uuid.uuid4().hex for _ in range(5)]
        self.bucket = 'issue20-archive'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('SEAL_')}
        self.origin = Origin(self.work)

    def clean(self, text):
        for secret in self.secrets:
            text = text.replace(secret, 'REDACTED')
        return text

    def check(self, label, success, **evidence):
        record = dict(assertion=label, status='PASS' if success else 'FAIL', **evidence)
        self.checks.append(record); print(record['status'], label, flush=True)
        write(self.artifacts / 'assertions.json', self.checks)

    def docker(self, *args):
        return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.PIPE).strip()

    def sql(self, query, params=(), dsn=None):
        with psycopg.connect(dsn or self.dsn, autocommit=True, row_factory=dict_row) as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def start(self):
        pg, minio = self.name + '-pg', self.name + '-minio'
        self.containers.extend([pg, minio])
        self.docker('run', '-d', '--name', pg, '-p', '127.0.0.1::5432', '-e',
                    'POSTGRES_PASSWORD=' + self.secrets[0], '-e', 'POSTGRES_DB=issue20',
                    '-v', f'{self.work / "postgres"}:/var/lib/postgresql/data', 'postgres:17-alpine')
        port = self.docker('port', pg, '5432').rsplit(':', 1)[1]
        self.dsn = f'postgresql://postgres:{self.secrets[0]}@127.0.0.1:{port}/issue20'
        self.docker('run', '-d', '--name', minio, '-p', '127.0.0.1::9000', '-e',
                    'MINIO_ROOT_USER=' + self.secrets[1], '-e', 'MINIO_ROOT_PASSWORD=' + self.secrets[2],
                    '-v', f'{self.work / "minio"}:/data', MINIO, 'server', '/data')
        port = self.docker('port', minio, '9000').rsplit(':', 1)[1]
        self.endpoint = f'http://127.0.0.1:{port}'
        self.s3 = boto3.client('s3', endpoint_url=self.endpoint,
            aws_access_key_id=self.secrets[1], aws_secret_access_key=self.secrets[2],
            region_name='us-east-1', config=Config(retries={'max_attempts': 0},
                connect_timeout=2, read_timeout=3, s3={'addressing_style': 'path'}))
        deadline = time.monotonic() + 45
        while True:
            try:
                self.sql('SELECT 1'); self.s3.list_buckets(); break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.2)
        self.s3.create_bucket(Bucket=self.bucket)
        policy = {'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Action': ['s3:ListBucket'], 'Resource': ['arn:aws:s3:::' + self.bucket]},
            {'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:PutObject'],
             'Resource': ['arn:aws:s3:::' + self.bucket + '/*']}]}
        write(self.work / 'policy.json', policy)
        command = ('mc alias set lab http://127.0.0.1:9000 "$ROOT_ID" "$ROOT_SECRET" >/dev/null && '
                   'mc admin user add lab "$APP_ID" "$APP_SECRET" >/dev/null && '
                   'mc admin policy create lab limited /input/policy.json >/dev/null && '
                   'mc admin policy attach lab limited --user "$APP_ID" >/dev/null')
        self.docker('run', '--rm', '--network', 'container:' + minio,
                    '-v', f'{self.work}:/input:ro', '-e', 'ROOT_ID=' + self.secrets[1],
                    '-e', 'ROOT_SECRET=' + self.secrets[2], '-e', 'APP_ID=' + self.secrets[3],
                    '-e', 'APP_SECRET=' + self.secrets[4], '--entrypoint', '/bin/sh', MC, '-c', command)
        self.env.update(SEAL_DATABASE_URL=self.dsn, SEAL_S3_ENDPOINT=self.endpoint,
            SEAL_S3_BUCKET=self.bucket, SEAL_S3_ACCESS_KEY=self.secrets[3],
            SEAL_S3_SECRET_KEY=self.secrets[4], SEAL_WORK_DIR=str(self.work / 'seal'),
            SEAL_ALLOW_LOOPBACK='1', NO_COLOR='1')
        self.origin.start()
        self.freeze()

    def freeze(self):
        sources = list((ROOT / 'src/seal').rglob('*.py')) + list((ROOT / 'src/seal').rglob('*.sql'))
        tests = list((ROOT / 'tests').glob('issue20*.py'))
        write(self.artifacts / 'environment.json', dict(
            command='uv run --no-sync python tests/issue20_e2e.py', python=sys.version,
            git_head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            postgres_image='postgres:17-alpine', minio_image=MINIO,
            minio_image_id=self.docker('image', 'inspect', MINIO, '--format', '{{.Id}}'),
            credentials='Random isolated DB/root/app credentials omitted; application List/Get/Put only',
            network='New loopback ports, isolated named containers; no existing service data used',
            scope='Synthetic real HTTP/browser inputs; all CLI, SQL and S3 consumers cross process',
            source_sha256={str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sorted(sources)},
            test_sha256={str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sorted(tests)}))

    def process(self, args, expected=0, env=None, label=None):
        argv = [sys.executable, *map(str, args)]
        p = subprocess.run(argv, cwd=ROOT, env=env or self.env, capture_output=True,
                           text=True, timeout=180)
        number = len(self.commands) + 1
        stdout, stderr = self.clean(p.stdout), self.clean(p.stderr)
        write(self.artifacts / f'{number:03d}-receipt.json', dict(stdout=stdout, stderr=stderr))
        self.commands.append(dict(argv=argv, exit=p.returncode, receipt=f'{number:03d}-receipt.json'))
        success = p.returncode == expected if isinstance(expected, int) else p.returncode in expected
        self.check(label or f'CLI/process {number} exits as expected', success, actual=p.returncode,
                   expected=expected)
        try:
            receipt = json.loads(stdout)
        except ValueError:
            receipt = {}
        return p.returncode, receipt

    def cli(self, *args, **options):
        return self.process(['-m', 'seal.cli', *args], **options)

    def config(self, ident, paths, mode='http', depth=0, robots=False, **crawl):
        path = self.work / (ident + '.yaml')
        data = dict(id=ident, entry_urls=[self.origin.base + p for p in paths],
                    allowed_hosts=['127.0.0.1'], allowed_path_prefixes=['/'],
                    archive_approved=True, scope='Authorized isolated Issue20 synthetic origin',
                    robots=robots, recheck_seconds=0, concurrency=1, delay=0.,
                    budget=dict(requests=100, seconds=90, response_bytes=1048576),
                    crawl4ai=dict(mode=mode, max_depth=depth, word_count_threshold=1, **crawl))
        path.write_text(yaml.safe_dump(data)); (self.artifacts / path.name).write_text(path.read_text())
        return path

    def rows(self, table, ident=None):
        if ident:
            return self.sql(f'SELECT t.* FROM seal.{table} t JOIN seal.resource r ON '
                'r.id=t.resource_id JOIN seal.frontier f ON f.id=r.frontier_id WHERE f.source_id=%s '
                'ORDER BY t.observed_at,t.id' if table == 'observation' else
                f'SELECT t.* FROM seal.{table} t JOIN seal.resource r ON r.id=t.resource_id '
                'JOIN seal.frontier f ON f.id=r.frontier_id WHERE f.source_id=%s ORDER BY t.sequence', (ident,))
        return self.sql(f'SELECT * FROM seal.{table}')

    def body(self, key):
        stream = self.s3.get_object(Bucket=self.bucket, Key=key)['Body']
        try:
            return stream.read()
        finally:
            stream.close()

    def objects(self):
        return [r['Key'] for page in self.s3.get_paginator('list_objects_v2').paginate(
            Bucket=self.bucket) for r in page.get('Contents', [])]

    def snapshot(self):
        return {table: self.rows(table) for table in ('source', 'frontier', 'run', 'resource',
                                                    'observation', 'version')}

    def close(self):
        self.origin.close()
        for name in reversed(self.containers):
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        write(self.artifacts / 'commands.json', self.commands)
        write(self.artifacts / 'summary.json', dict(
            passed=sum(c['status'] == 'PASS' for c in self.checks),
            failed=sum(c['status'] == 'FAIL' for c in self.checks),
            command='uv run --no-sync python tests/issue20_e2e.py',
            artifacts=str(self.artifacts), work=str(self.work),
            unverified=['Public websites, production upgrade, OSS compatibility, OCR/Office parsing',
                        'Whole-site completeness UNKNOWN; synthetic bounded origin only']))
        print('ARTIFACTS', self.artifacts, flush=True)
