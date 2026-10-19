"""Apply root-approved visual corpus corrections before OCR; idempotent replay."""
import json
from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'artifacts/tesseract-multilang-20261016'
proposal = json.loads((BASE / 'corpus-review/proposed-corrections.json').read_text())
audit = []
for language in ('chi_sim', 'chi_tra', 'eng'):
    path = BASE / language / 'references.json'
    refs = json.loads(path.read_text())
    before_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    for correction in proposal['corrections']:
        if correction['language'] != language:
            continue
        region = next(r for page in refs['pages'] for r in page['regions'] if r['id'] == correction['region_id'])
        old = {key: region[key] for key in correction['before']}
        assert old in (correction['before'], correction['after']), correction['region_id']
        region.update(correction['after'])
        audit.append({'region_id': correction['region_id'], 'status': 'applied' if old == correction['before'] else 'already_applied', 'before': old, 'after': correction['after']})
    path.write_text(json.dumps(refs, ensure_ascii=False, indent=2) + '\n')
    audit.append({'language': language, 'before_sha256': before_sha, 'after_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
output = BASE / 'corpus-review/applied-corrections.json'
if output.exists():
    assert all(row.get('status') in (None, 'already_applied') for row in audit), 'Audit exists but fresh corrections detected'
else:
    output.write_text(json.dumps({'approval': 'Root agent visually confirmed all six corrections before OCR and explicitly authorized application', 'changes': audit}, ensure_ascii=False, indent=2) + '\n')
