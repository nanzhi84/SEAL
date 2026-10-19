"""Freeze exact observed-text locations; never uses references to choose a span."""
import hashlib
import json
from pathlib import Path
import unicodedata

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/tesseract-frontier-20261016'
def norm(s):
    return ' '.join(unicodedata.normalize('NFC',s).split())
def locations(text, needle):
    positions=[]
    start=0
    while True:
        p=text.find(needle,start)
        if p<0:
            return positions
        positions.append([p,p+len(needle)])
        start=p+1

def main():
    rows=[]
    for group in ['01-12','13-24','25-36']:
        source=ROOT/f'artifacts/mineru-vlm-full-review-20261016/review-{group}.json'
        for page in json.loads(source.read_text())['pages']:
            page_no=page['page']
            baseline=OUT/f'fast-144/page-{page_no:02}.txt'
            text=norm(baseline.read_text())
            for index,check in enumerate(page['spot_checks'],1):
                observed=norm(check['tesseract_observed'])
                found=locations(text,observed)
                rows.append({'reference_id':f'{page_no:02}-{index}','page':page_no,'index':index,'reference':check['reference'],'observed_anchor':observed,'status':'unique_exact' if len(found)==1 else 'unresolved','unresolved_reason':None if len(found)==1 else ('multiple_exact_occurrences' if found else 'no_exact_occurrence'),'candidate_spans':found,'baseline_span':found[0] if len(found)==1 else None,'baseline_sha256':hashlib.sha256(baseline.read_bytes()).hexdigest(),'baseline_context':text[max(0,found[0][0]-50):found[0][1]+50] if len(found)==1 else None})
    result={'normalization':'Unicode NFC and whitespace collapse','purpose':'observed anchors provide location only; frozen reference remains truth','count':len(rows),'unique_exact':sum(r['status']=='unique_exact' for r in rows),'unresolved':[r['reference_id'] for r in rows if r['status']=='unresolved'],'anchors':rows}
    path=OUT/'baseline-anchor-audit.json'
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='anchors'},ensure_ascii=False))
if __name__=='__main__':
    main()
