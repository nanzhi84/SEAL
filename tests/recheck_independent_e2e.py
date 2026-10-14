"""Independent real CLI acceptance, reuse existing isolated service harness.
Failure contracts: stable bytes invent a Version or lose Observation; changed bytes
lose Version; private address/path escape causes HTTP requests or successful data.
Run: uv run --no-sync python tests/recheck_independent_e2e.py
"""
import shutil
import sys
import traceback
import yaml
from pathlib import Path
from issue20_lab import ROOT, digest, write
from issue20_e2e import Acceptance

class Recheck(Acceptance):
    def behavior(self):
        path = self.config('dd-017', ['/article'])
        self.cli('collect', path, '--no-progress')
        versions = self.rows('version', 'dd-017')
        observations = self.rows('observation', 'dd-017')
        self.check('initial collection makes one Version and one Observation', len(versions)==1 and len(observations)==1)
        self.cli('recheck', path, '--no-progress')
        after = self.rows('observation', 'dd-017')
        self.check('same bytes recheck adds exactly one network Observation', len(after)==len(observations)+1 and after[-1]['outcome']=='unchanged')
        self.check('same bytes recheck preserves every original Version field', self.rows('version', 'dd-017')==versions)
        self.check('same bytes verified by actual HTTP and stored body hash', self.origin.count('/article')==2 and all(o['body_hash']==digest(self.origin.alpha) for o in after))
        self.check('unchanged result available over actual S3 API', bool(after[-1]['result_ref']) and bool(self.body(after[-1]['result_ref'])))
        self.origin.set('/article', self.origin.beta)
        self.cli('recheck', path, '--no-progress')
        changed = self.rows('version', 'dd-017')
        self.check('changed bytes append one Version with next sequence', len(changed)==2 and [v['sequence'] for v in changed]==[1,2] and changed[-1]['body_hash']==digest(self.origin.beta))
        self.check('changed bytes append own Observation', len(self.rows('observation','dd-017'))==3)
        self.origin.set('/article', self.origin.alpha)
        self.cli('recheck', path, '--no-progress')
        self.check('A to B to A appends third Version', [v['body_hash'] for v in self.rows('version','dd-017')]==[digest(self.origin.alpha),digest(self.origin.beta),digest(self.origin.alpha)])
        write(self.artifacts/'timeline.json', dict(versions=self.rows('version','dd-017'),observations=self.rows('observation','dd-017'),http=self.origin.requests))
        blocked=self.config('dd-002',['/denied'])
        before=self.origin.count('/denied')
        self.cli('collect',blocked,'--no-progress',expected=(1,2),env={**self.env,'SEAL_ALLOW_LOOPBACK':'0'})
        self.check('private loopback address rejected without HTTP request',self.origin.count('/denied')==before and not self.rows('version','dd-002'))
        scope=self.config('dd-003',['/denied'])
        data=yaml.safe_load(scope.read_text());data['allowed_path_prefixes']=['/article'];scope.write_text(yaml.safe_dump(data))
        self.cli('collect',scope,'--no-progress',expected=(1,2))
        self.check('entry outside allowed path rejected without HTTP request',self.origin.count('/denied')==before and not self.rows('version','dd-003'))

lab=Recheck()
lab.artifacts=ROOT/'artifacts/recheck-independent-20261014';lab.artifacts.mkdir(parents=True,exist_ok=True)
try:
    lab.start()
    metadata=__import__('json').loads((lab.artifacts/'environment.json').read_text())
    metadata['command']='uv run --no-sync python tests/recheck_independent_e2e.py'
    metadata['test_sha256']['tests/recheck_independent_e2e.py']=digest(Path(__file__).read_bytes())
    write(lab.artifacts/'environment.json',metadata)
    lab.cli('init');lab.behavior()
except Exception as error:
    lab.check('acceptance completes without exception',False,error=lab.clean(str(error)))
    traceback.print_exc()
finally:
    lab.close()
    summary=__import__('json').loads((lab.artifacts/'summary.json').read_text());summary['command']='uv run --no-sync python tests/recheck_independent_e2e.py';write(lab.artifacts/'summary.json',summary)
    shutil.copy(lab.origin.log,lab.artifacts/'http-requests.jsonl') if lab.origin.log.exists() else None
sys.exit(any(c['status']=='FAIL' for c in lab.checks))
