"""Verify the maintained source register and its explainer without network or a database."""
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from parsel import Selector

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def main():
    checks = []

    def check(name, actual, expected):
        checks.append({'name': name, 'actual': actual, 'expected': expected,
                       'status': 'PASS' if actual == expected else 'FAIL'})

    reference = ROOT / 'docs/reference/source-adaptation.md'
    generated = ROOT / 'docs/generated/due-diligence-mvp.html'
    text = reference.read_text()
    records = [[cell.strip() for cell in line.split('|')[1:-1]]
               for line in text.splitlines() if re.match(r'^\| dd-\d{3} \|', line)]
    evidence = json.loads((HERE / 'results/classification/entries.json').read_text())
    check('complete_stable_ids', sorted(r[0] for r in records), sorted(r['id'] for r in evidence))
    check('unique_ids', len({r[0] for r in records}), 491)
    check('eleven_complete_columns', all(len(r) == 11 and all(r) for r in records), True)
    check('classification_counts', dict(Counter(r[1].split('／')[0] for r in records)),
          dict(Counter(r['class'] for r in evidence)))
    check('grouped_by_class', [r[1].split('／')[0] for r in records],
          sorted(r[1].split('／')[0] for r in records))
    adaptation = {'未适配', '适配中', '待验收', '已适配', '阻塞', '排除'}
    audit = {'未审计', '审计中', '通过', '需修正', '不适用'}
    check('valid_workflow_states', all(r[7] in adaptation and r[8] in audit for r in records), True)
    check('completed_rows_have_evidence', all(r[9] != '—' and '未分配' not in r[10]
          for r in records if r[7] == '已适配' or r[8] == '通过'), True)
    check('single_reference_document', sorted(p.name for p in (ROOT/'docs/reference').glob('*.md')),
          ['source-adaptation.md'])
    check('research_removed_from_plans', (ROOT/'docs/plans/due-diligence-mvp.md').exists(), False)
    original = ROOT/'experiments/due-diligence/inputs/source-map.md'
    manifest = json.loads((HERE/'results/20261009/manifest.json').read_text())
    check('input_snapshot_bytes_preserved', hashlib.sha256(original.read_bytes()).hexdigest(),
          manifest['map_sha256'])
    selector = Selector(text=generated.read_text())
    draft = selector.css('#am-source::text').get() or ''
    check('skill_embedded_markdown', bool(draft), True)
    check('eight_explainer_panels', len(re.findall(r'^## ', draft, re.M)), 8)
    check('html_points_to_canonical_register', '../reference/source-adaptation.md' in draft, True)
    check('html_has_no_removed_plan', 'plans/due-diligence-mvp.md' in generated.read_text(), False)
    # Resolve actual links, including those in the embedded source, independently of seiso lint.
    broken = []
    files = [reference, ROOT/'README.md', ROOT/'experiments/due-diligence/reference-snapshot/due-diligence-source-experiment.md']
    for path, content in [(p, p.read_text()) for p in files] + [(generated, draft)]:
        for target in re.findall(r'\]\(([^)]+)\)', content):
            parsed = urlsplit(target)
            if parsed.scheme or not parsed.path:
                continue
            dest = path.parent / unquote(parsed.path)
            if not dest.exists():
                broken.append({'file': str(path.relative_to(ROOT)), 'target': target})
    check('local_document_links_resolve', broken, [])
    result = {
        'scope': 'offline source register coverage, workflow evidence and document navigation',
        'command': 'uv run --frozen python experiments/source-accessibility/verify_reference.py',
        'environment': 'project Python 3.12 and uv.lock; frozen public fixtures; no network or database',
        'summary': {'assertions': len(checks), 'passed': sum(c['status'] == 'PASS' for c in checks),
                    'failed': sum(c['status'] == 'FAIL' for c in checks)},
        'artifacts': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in [reference, generated, original]},
        'checks': checks,
    }
    out = HERE/'results/classification/reference-verification.json'
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(result['summary']))
    for c in checks:
        if c['status'] == 'FAIL':
            print(json.dumps(c, ensure_ascii=False))
    return bool(result['summary']['failed'])


if __name__ == '__main__':
    raise SystemExit(main())
