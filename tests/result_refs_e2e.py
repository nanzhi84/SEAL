"""Independent E2E. Failures: lost/mismatched references, unchanged version churn,
failed SDK advancing current, missing/corrupt/mismatched old evidence partially
committing migration, cursor/time identity drift, rewritten manifests, repeated init.
Run: uv run --no-sync python tests/result_refs_e2e.py
Requires local Docker images and pg_dump/pg_restore; isolated loopback only.
"""
import json
import sys
import subprocess
from pathlib import Path
from psycopg.types.json import Jsonb
from s3_storage_e2e import Lab, digest, write, ROOT

class Acceptance(Lab):
    def start(self):
        super().start()
        file=self.artifacts / "environment.json"
        metadata=json.loads(file.read_text())
        metadata['product_source_sha256'].update({str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in sorted((ROOT/'src/seal/migrations').glob('*.sql'))})
        metadata['test_source_sha256']={str(p.relative_to(ROOT)):digest(p.read_bytes()) for p in (Path(__file__),ROOT/'tests/s3_storage_e2e.py',ROOT/'tests/issue17_fixture.py')}
        metadata.update(command="uv run --no-sync python tests/result_refs_e2e.py",test_sha256=digest(Path(__file__).read_bytes()),legacy_fixture="Real CLI collected data converted to exact 0010 columns and ledger, not historical executable")
        write(file,metadata)

    def rows(self):
        return self.sql('SELECT * FROM seal.observation ORDER BY observed_at,id')

    def snapshot(self):
        return {t:self.sql(f'SELECT row_to_json(t) AS row FROM seal.{t} t ORDER BY id')
                for t in ('observation','version','resource','run','frontier')}

    def rebuild(self):
        code='''import os,json,hashlib
import psycopg
from psycopg.rows import dict_row
from seal.objects import Objects
from seal.archive import deserialize_result
with psycopg.connect(os.environ['SEAL_DATABASE_URL'],row_factory=dict_row) as c:
 rows=c.execute('SELECT * FROM seal.observation WHERE result_ref IS NOT NULL ORDER BY observed_at,id').fetchall()
out=[]
for row in rows:
 envelope=Objects().json(row['result_ref']);r=deserialize_result(envelope)
 out.append({'id':row['id'],'success':r.success,'url':r.url,'result_ref':row['result_ref'],'outcome':row['outcome'],'markdown':str(r.markdown),'html':r.html,'body_ok':not row['body_hash'] or hashlib.sha256(Objects().get(row['body_hash'])).hexdigest()==row['body_hash']})
print(json.dumps({'results':out}))'''
        _, receipt=self.process(['-c',code])
        self.check('new process reconstructs each referenced native SDK result and original body',
                   len(receipt.get('results',[]))==len([r for r in self.rows() if r['result_ref']]) and all(r['body_ok'] for r in receipt.get('results',[])))
        self.check('cross-process reconstructed successful SDK HTML and Markdown visible',any(r['success'] and r['html'] and r['markdown'] and r['markdown']!='None' for r in receipt.get('results',[])))
        write(self.artifacts/'reconstructed.json',receipt)

    def old_shape(self):
        observations=self.rows()
        envelopes={r['id']:json.loads(self.body(r['result_ref'])) for r in observations if r['result_ref']}
        self.sql('DROP VIEW seal.current_resource_version; DROP VIEW seal.version_delta')
        self.sql('ALTER TABLE seal.version DISABLE TRIGGER version_immutable; ALTER TABLE seal.observation DISABLE TRIGGER observation_immutable')
        self.sql('ALTER TABLE seal.version ADD COLUMN crawl_result jsonb')
        for v in self.sql('SELECT * FROM seal.version'):
            self.sql('UPDATE seal.version SET crawl_result=%s WHERE id=%s',(Jsonb(envelopes[v['observation_id']]),v['id']))
        for r in self.sql('SELECT id,report FROM seal.run'):
            report=dict(r['report'] or {})
            report['crawl_results']={o['id']:o['result_ref'] for o in observations if o['run_id']==r['id'] and o['result_ref']}
            self.sql('UPDATE seal.run SET report=%s WHERE id=%s',(Jsonb(report),r['id']))
        self.sql('ALTER TABLE seal.version DROP COLUMN result_ref CASCADE; ALTER TABLE seal.observation DROP COLUMN result_ref CASCADE; ALTER TABLE seal.version ALTER COLUMN crawl_result SET NOT NULL')
        self.sql('ALTER TABLE seal.version ENABLE TRIGGER version_immutable; ALTER TABLE seal.observation ENABLE TRIGGER observation_immutable')
        self.sql('CREATE VIEW seal.current_resource_version AS SELECT f.source_id,r.url,v.* FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id JOIN seal.version v ON v.id=r.current_version_id')
        self.sql('CREATE VIEW seal.version_delta AS SELECT f.source_id,r.url,v.* FROM seal.version v JOIN seal.resource r ON r.id=v.resource_id JOIN seal.frontier f ON f.id=r.frontier_id')
        self.sql("DELETE FROM seal.migration WHERE name='0011_result_references.sql'")
        return observations,envelopes

    def execute(self):
        self.cli('init')
        path=self.source(paths=('/article?edition=1',))
        self.cli('collect',path,'--no-progress')
        before=self.sql('SELECT * FROM seal.version ORDER BY cursor')
        self.check('initial successful native version',bool(before))
        self.cli('recheck',path,'--no-progress')
        unchanged=[r for r in self.rows() if r['outcome']=='unchanged']
        self.check('unchanged SDK observation has own archived result',bool(unchanged) and all(r['result_ref'] for r in unchanged))
        self.check('unchanged retains original version identity and original result',self.sql('SELECT * FROM seal.version ORDER BY cursor')==before)
        self.check('unchanged observation distinct result from originating version',any(r['result_ref']!=before[0]['result_ref'] for r in unchanged))
        self.fixture.set('/article?edition=1',status=500,body=b'Fixture temporary failure')
        self.cli('recheck',path,'--no-progress',expected=1)
        failed=[r for r in self.rows() if r['outcome'] not in ('parsed','unchanged')]
        self.check('failed SDK result preserved without advancing current version',bool(failed) and all(r['result_ref'] for r in failed) and self.sql('SELECT * FROM seal.version ORDER BY cursor')==before)
        self.rebuild()
        oldobs,envelopes=self.old_shape()
        original=self.snapshot()
        write(self.artifacts/'0010-before.json',original)
        keys=self.objects(); original_objects={k:self.body(k) for k in keys}
        oldref=next(o['result_ref'] for o in oldobs if o['result_ref'])
        for label in ('missing','corrupt'):
            if label=='missing': self.s3.delete_object(Bucket=self.bucket,Key=oldref)
            else:self.s3.put_object(Bucket=self.bucket,Key=oldref,Body=b'corrupt')
            self.cli('init','--backup',self.artifacts/(label+'.dump'),expected=1)
            self.check(label+' historical result causes atomic rollback',self.snapshot()==original and not self.sql("SELECT * FROM seal.migration WHERE name='0011_result_references.sql'"))
            self.s3.put_object(Bucket=self.bucket,Key=oldref,Body=original_objects[oldref])
        first=self.sql('SELECT r.id,r.report FROM seal.run r JOIN seal.observation o ON o.run_id=r.id JOIN seal.version v ON v.observation_id=o.id')[0]
        report=dict(first['report']); mapping=dict(report['crawl_results'])
        oid=next(iter(mapping)); wrong=dict(envelopes[oid]); wrong['marker']='inconsistent old mapping'
        raw=json.dumps(wrong,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode(); key=digest(raw)
        self.s3.put_object(Bucket=self.bucket,Key=key,Body=raw)
        mapping[oid]=key;report['crawl_results']=mapping
        self.sql('UPDATE seal.run SET report=%s WHERE id=%s',(Jsonb(report),first['id']))
        inconsistent=self.snapshot()
        self.cli('init','--backup',self.artifacts/'inconsistent.dump',expected=1)
        self.check('inconsistent mapping and version JSON atomically rejected',self.snapshot()==inconsistent)
        self.sql('UPDATE seal.run SET report=%s WHERE id=%s',(Jsonb(first['report']),first['id']))
        self.cli('init','--backup',self.artifacts/'good.dump')
        self.sql('CREATE DATABASE seal_backup_check')
        live_dsn=self.dsn
        restore_dsn=live_dsn.rsplit('/',1)[0]+'/seal_backup_check'
        restored=subprocess.run(['pg_restore','--exit-on-error','--dbname',restore_dsn,str(self.artifacts/'good.dump')],capture_output=True,text=True)
        self.check('backup actually restores into independent database',restored.returncode==0)
        self.dsn=restore_dsn
        self.check('restored backup preserves exact 0010 data before upgrade',self.snapshot()==original)
        self.dsn=live_dsn
        after=self.snapshot();write(self.artifacts/'0011-after.json',after)
        for table,records in original.items():
            candidate=after[table]
            for entry in records:
                old=dict(entry['row']);oid=old['id']
                old.pop('crawl_result',None)
                if table=='run': old['report']=dict(old['report']);old['report'].pop('crawl_results',None)
                new=next(r['row'] for r in candidate if r['row']['id']==oid)
                self.check('migration preserves original fields '+table+' '+oid,all(new.get(k)==v for k,v in old.items()))
        migrated={r['id']:r for r in self.rows()}
        self.check('all historical SDK observations migrate exact envelope references',all(migrated[o['id']]['result_ref']==o['result_ref'] for o in oldobs))
        self.check('old manifest bytes unchanged',all(self.body(k)==v for k,v in original_objects.items()))
        self.check('version result reference equals originating observation',all(v['result_ref']==migrated[v['observation_id']]['result_ref'] for v in self.sql('SELECT * FROM seal.version')))
        self.check('old relational result field removed',not self.sql("SELECT * FROM information_schema.columns WHERE table_schema='seal' AND table_name='version' AND column_name='crawl_result'"))
        self.check('run result mapping removed',not self.sql("SELECT id FROM seal.run WHERE report ? 'crawl_results'"))
        self.rebuild()
        self.attachment_roundtrip()
        stable=self.snapshot();objects=set(self.objects())
        self.cli('init')
        self.check('repeated init leaves rows and objects unchanged',self.snapshot()==stable and set(self.objects())==objects)
        write(self.artifacts/'objects.json',[{'key':k,'sha256':digest(self.body(k)),'bytes':len(self.body(k))} for k in self.objects()])

if __name__=='__main__':
    lab=Acceptance()
    try:
        lab.start();lab.execute()
    except Exception as exc:
        lab.check('acceptance completed',False,category=type(exc).__name__,detail=lab.clean(str(exc)))
    finally:
        lab.close()
        summary=json.loads((lab.artifacts/'summary.json').read_text());summary['rerun']='uv run --no-sync python tests/result_refs_e2e.py';write(lab.artifacts/'summary.json',summary)
    sys.exit(int(any(c['status']=='FAIL' for c in lab.checks)))
