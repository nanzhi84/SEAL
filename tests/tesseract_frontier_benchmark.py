"""Reproducible serial benchmark; run with uv run --no-sync python tests/tesseract_frontier_benchmark.py."""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import time
import unicodedata

import numpy as np
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/tesseract-frontier-20261016'
WORK = ROOT / 'work/tesseract-frontier-20261016'
PDF = ROOT / 'artifacts/dd-492-local-ocr-20261015-194700/dd-492-01-original.pdf'

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def norm(text):
    return ' '.join(unicodedata.normalize('NFC', text).split())

def align(reference, text):
    """Unit-cost semi-global Levenshtein: free text prefix/suffix, full reference.

    DP[0,j]=0; DP[i,0]=i. Each row's insertion recurrence is evaluated
    exactly using a prefix minimum. Earliest minimum endpoint wins ties;
    traceback prefers diagonal, deletion, then insertion. Offsets refer to
    NFC/whitespace-normalized Unicode code points, not original OCR bytes.
    """
    reference, text = norm(reference), norm(text)
    if not reference:
        raise ValueError('empty reference')
    n = len(text)
    cols = np.arange(1, n + 1, dtype=np.int32)
    chars = np.array(list(text))
    dp = np.zeros((len(reference) + 1, n + 1), dtype=np.int32)
    dp[:, 0] = np.arange(len(reference) + 1)
    for i, ch in enumerate(reference, 1):
        base = np.minimum(dp[i-1, 1:] + 1, dp[i-1, :-1] + (chars != ch))
        dp[i, 1:] = np.minimum.accumulate(np.minimum(base - cols, i)) + cols
    end = int(np.argmin(dp[-1]))
    distance = int(dp[-1, end])
    i, j = len(reference), end
    while i:
        if j and dp[i,j] == dp[i-1,j-1] + (reference[i-1] != text[j-1]):
            i, j = i-1, j-1
        elif dp[i,j] == dp[i-1,j] + 1:
            i -= 1
        else:
            j -= 1
    return {'distance': distance, 'reference_characters': len(reference),
            'cer': distance / len(reference), 'start': j, 'end': end,
            'matched_text': text[j:end], 'reference': reference}

def scorer_checks():
    cases = [('abc','unrelated abc tail',0,'abc'),('abc','xx axc yy',1,'axc'),
             ('abc','xx ac yy',1,'ac'),('abcd','xx abxcd yy',1,'abxcd'),
             ('abc','cba',2,None),('abc','a--b--c',2,None),
             ('ế','xx ế yy',0,'ế'),('a b','xx a\n   b yy',0,'a b'),
             ('abc','',3,''),('abc','abc abc',0,'abc')]
    results = []
    for ref, text, distance, match in cases:
        got = align(ref, text)
        assert got['distance'] == distance, (ref,text,got)
        if match is not None:
            assert got['matched_text'] == match, (ref,text,got)
        results.append({'input_reference':ref,'input_text':text,'expected_distance':distance,'result':got})
    save(OUT / 'scorer-contract-checks.json', {'passed':True,'cases':results})

def freeze():
    path = OUT / 'references.json'
    if path.exists():
        return json.loads(path.read_text())
    sources, refs = [], []
    for group in ['01-12','13-24','25-36']:
        src = ROOT / f'artifacts/mineru-vlm-full-review-20261016/review-{group}.json'
        sources.append({'path':str(src.relative_to(ROOT)),'sha256':sha(src)})
        for page in json.loads(src.read_text())['pages']:
            for index, check in enumerate(page['spot_checks'],1):
                refs.append({'page':page['page'],'index':index,'reference':check['reference']})
    assert len(refs) == 108
    frozen = {'sources':sources,'count':len(refs),'references':refs,'normalization':'Unicode NFC, split/join whitespace; preserve case and punctuation','algorithm':'semi-global unit-cost Levenshtein, entire reference against any contiguous normalized page substring; earliest endpoint; diagonal/deletion/insertion traceback ties','limitations':json.loads((OUT/'boundaries.json').read_text())['acceptance']['limitations']}
    save(path, frozen)
    return frozen

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--resume',action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    refs = freeze()
    scorer_checks()
    if args.prepare_only:
        print('Frozen 108 quotations; scorer behavior checks PASS',flush=True)
        return
    models = {name:WORK/'models'/name/'vie.traineddata' for name in ['fast','best']}
    identity = {'pdf_sha256':sha(PDF),'reference_sha256':sha(OUT/'references.json'),
                'models':{k:sha(v) for k,v in models.items()},'platform':platform.platform(),
                'processor':platform.processor(),'machine':platform.machine(),
                'tesseract_version':subprocess.check_output(['tesseract','--version'],text=True,stderr=subprocess.STDOUT),
                'python':platform.python_version(),'cpu_count':os.cpu_count(),
                'measurement_started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'configuration_order':['fast-144','fast-200','fast-240','fast-300','best-144','best-200','best-240','best-300','fast-96','fast-120','fast-168','best-120'],'explicit_dpi':True,'execution':'config serial, page serial, OMP_THREAD_LIMIT=1; no warmup; no image cache; one measured trial'}
    for command,key in [(['sysctl','-n','machdep.cpu.brand_string'],'cpu'),(['sysctl','-n','hw.memsize'],'memory_bytes')]:
        identity[key]=subprocess.check_output(command,text=True).strip()
    old = OUT/'environment.json'
    if args.resume and old.exists():
        previous=json.loads(old.read_text())
        for key in ['pdf_sha256','reference_sha256','models','tesseract_version']:
            assert previous[key]==identity[key],f'resume identity mismatch {key}'
    if args.resume and old.exists():
        identity['prior_measurement']=json.loads(old.read_text())
    save(old,identity)
    doc = pdfium.PdfDocument(str(PDF))
    assert len(doc)==36
    configs=[]
    configurations=[(model,dpi) for model in models for dpi in [144,200,240,300]] + [('fast',96),('fast',120),('fast',168),('best',120)]
    for model,dpi in configurations:
        if True:
            name=f'{model}-{dpi}'
            rows=[]
            for p in range(1,37):
                result_path=OUT/name/f'page-{p:02}.json'
                if args.resume and result_path.exists():
                    rows.append(json.loads(result_path.read_text()))
                    continue
                row={'page':p,'model':model,'dpi':dpi,'status':'pending','started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'explicit_dpi':True}
                png=WORK/name/f'page-{p:02}.png'
                png.parent.mkdir(parents=True,exist_ok=True)
                try:
                    started=time.perf_counter()
                    page=doc[p-1]
                    bitmap=page.render(scale=dpi/72)
                    image=bitmap.to_pil()
                    image.save(png)
                    image.close(); bitmap.close(); page.close()
                    row['render_seconds']=time.perf_counter()-started
                    command=['tesseract',str(png),'stdout','--tessdata-dir',str(models[model].parent),'-l','vie','--dpi',str(dpi),'--oem','1','--psm','3']
                    started=time.perf_counter()
                    try:
                        proc=subprocess.run(command,capture_output=True,text=True,timeout=180,env={**os.environ,'OMP_THREAD_LIMIT':'1'})
                    finally:
                        row['ocr_seconds']=time.perf_counter()-started
                    row.update(returncode=proc.returncode,stderr=proc.stderr)
                    if proc.returncode:
                        row['status']='ocr_failed'
                    elif not proc.stdout.strip():
                        row['status']='empty_output'
                    else:
                        text_path=result_path.with_suffix('.txt')
                        text_path.parent.mkdir(parents=True,exist_ok=True)
                        text_path.write_text(proc.stdout)
                        row.update(status='ok',text_sha256=sha(text_path),normalized_characters=len(norm(proc.stdout)))
                except subprocess.TimeoutExpired:
                    row['status']='timeout'
                except Exception as exc:
                    row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
                finally:
                    png.unlink(missing_ok=True)
                row['total_seconds']=row.get('render_seconds',0)+row.get('ocr_seconds',0)
                save(result_path,row)
                rows.append(row)
                print(f'{name} page {p}/36 {row["status"]} {row["total_seconds"]:.3f}s',flush=True)
            matches=[]
            complete=all(r['status']=='ok' for r in rows)
            config={'configuration':name,'model':model,'dpi':dpi,'status':'complete' if complete else 'incomplete','successful_pages':sum(r['status']=='ok' for r in rows),'render_seconds':sum(r.get('render_seconds',0) for r in rows),'ocr_seconds':sum(r.get('ocr_seconds',0) for r in rows),'total_seconds':sum(r['total_seconds'] for r in rows),'matched_quotations':len(matches),'reference_characters':sum(m['reference_characters'] for m in matches),'edit_distance':sum(m['distance'] for m in matches)}
            config['fixed_quotation_sample_cer']=None
            configs.append(config)
            save(OUT/'summary.partial.json',configs)
    print('All timed OCR finished; scoring frozen quotations now',flush=True)
    for config in configs:
        matches=[]
        for p in range(1,37):
            result_path=OUT/config['configuration']/f'page-{p:02}.json'
            row=json.loads(result_path.read_text())
            if row['status']=='ok':
                output=result_path.with_suffix('.txt').read_text()
                row['unanchored_matches_superseded']=[align(r['reference'],output) | {'index':r['index']} for r in refs['references'] if r['page']==p]
                matches.extend(row['unanchored_matches_superseded'])
                save(result_path,row)
        config['matched_quotations']=len(matches)
        config['reference_characters']=sum(m['reference_characters'] for m in matches)
        config['edit_distance']=sum(m['distance'] for m in matches)
        config['fixed_quotation_sample_cer']=config['edit_distance']/config['reference_characters'] if config['status']=='complete' else None
    complete=[c for c in configs if c['status']=='complete']
    for c in configs:
        c['pareto_nondominated']=c['status']=='complete' and not any(other['total_seconds']<=c['total_seconds'] and other['fixed_quotation_sample_cer']<=c['fixed_quotation_sample_cer'] and (other['total_seconds']<c['total_seconds'] or other['fixed_quotation_sample_cer']<c['fixed_quotation_sample_cer']) for other in complete)
    save(OUT/'raw-summary.unanchored.json',{'configurations':configs,'metric':'fixed quotation sample CER, character weighted; not full-text CER','references':108,'resume_requested':args.resume,'limitations':refs['limitations']})
    lines=['# 越南 PDF Tesseract 实测基准','','固定引文样本 CER = 108 条引文最小编辑距离之和 / 归一化引文字符数之和；不是全文 CER。','','|配置|状态|渲染秒|OCR秒|合计秒|固定引文样本CER|非支配|','|---|---|---:|---:|---:|---:|---|']
    for c in configs:
        cer=c['fixed_quotation_sample_cer']
        lines.append(f'|{c["configuration"]}|{c["status"]}|{c["render_seconds"]:.3f}|{c["ocr_seconds"]:.3f}|{c["total_seconds"]:.3f}|{cer:.6f}|{c["pareto_nondominated"]}|' if cer is not None else f'|{c["configuration"]}|incomplete||||||')
    lines+=['','运行：`uv run --no-sync python tests/tesseract_frontier_benchmark.py`；断点续跑加 `--resume`。','环境与PDF及模型哈希见 environment.json；运行前冻结的引文及来源哈希见 references.json；逐页输出、耗时和匹配区间见配置子目录。','','每次配置重新渲染并编码PNG；OCR包含子进程启动并显式传入 --dpi；评分在全部配置完成后执行且不计时。配置顺序为 fast144/200/240/300、best144/200/240/300，随后另一个时段扩展 fast96/120/168、best120；未交错，热漂移和系统负载未受控。单配置/单页串行；OMP_THREAD_LIMIT=1；OEM1 PSM3 vie；每页OCR超时180秒。PNG结束后删除；文本与JSON保留。','计时为一次实测，不含模型下载；预先启动的其他系统负载未受控。失败配置不进入帕累托比较。','','局限：']+['- '+s for s in refs['limitations']]
    (OUT/'raw-report.unanchored.md').write_text('\n'.join(lines)+'\n')
    doc.close()

if __name__=='__main__':
    main()
