"""Location-constrained scoring of frozen quotations, separate from timed OCR."""
import difflib
import hashlib
from functools import lru_cache
import json
from pathlib import Path
import shutil

from tesseract_frontier_benchmark import OUT, align, norm, save


@lru_cache(maxsize=2)
def page_opcodes(baseline,target):
    return difflib.SequenceMatcher(None,baseline,target,autojunk=False).get_opcodes()

def mapped_span(baseline, target, span):
    """Map baseline boundaries through a monotone whole-page character alignment."""
    start,end=span
    opcodes=page_opcodes(baseline,target)
    overlaps=[op for op in opcodes if op[2]>start and op[1]<end]
    if overlaps and all(op[0]=='delete' for op in overlaps):
        return None,{'reason':'anchor_deleted','opcodes':overlaps}
    def boundary(position, is_end):
        candidates=[]
        for tag,a,b,c,d in opcodes:
            if a<=position<=b and b>a:
                mapped=c+round((position-a)*(d-c)/(b-a))
                candidates.append(mapped)
        return (min(candidates) if is_end else max(candidates)) if candidates else None
    left,right=boundary(start,False),boundary(end,True)
    equal=sum(max(0,min(end,b)-max(start,a)) for tag,a,b,c,d in overlaps if tag=='equal')
    details={'opcodes':overlaps,'equal_fraction':equal/(end-start),'algorithm':'difflib.SequenceMatcher autojunk=False, normalized full page, monotone boundary interpolation through replacement opcodes'}
    if left is None or right is None or right<=left:
        return None,details|{'reason':'collapsed_or_unmapped_span'}
    return [left,right],details


def score_one(reference, baseline, target, span):
    mapped,details=mapped_span(baseline,target,span)
    result={'baseline_span':span,'mapped_span':mapped,'mapping':details}
    if mapped is None:
        if details.get('reason')=='anchor_deleted':
            length=len(norm(reference))
            return result|{'status':'scored','confirmed_deletion':True,'window_span':None,'matched_span':None,'window_text':'','window_boundary_contact':False,'match':{'distance':length,'reference_characters':length,'cer':1.0,'reference':norm(reference),'matched_text':'','start':0,'end':0}}
        return result|{'status':'unresolved'}
    start,end=mapped
    ws,we=max(0,start-8),min(len(target),end+8)
    match=align(reference,target[ws:we])
    result.update(window_span=[ws,we],window_text=target[ws:we],matched_span=[ws+match['start'],ws+match['end']],match=match)
    if we-ws<len(norm(reference))/2:
        return result|{'status':'unresolved','reason':'mapped_window_too_short'}
    result.update(status='scored',window_boundary_contact=(match['start']==0 and ws>0) or (match['end']==we-ws and we<len(target)))
    return result


def contracts():
    baseline='đầu một số điều theo Luật số 65/2020/QH14; đoạn khác một số điều theo Luật số 63/2020/0H14; cuối'
    reference='một số điều theo Luật số 63/2020/QH14;'
    observed='một số điều theo Luật số 63/2020/0H14;'
    span=[baseline.index(observed),baseline.index(observed)+len(observed)]
    target=baseline.replace('63/2020/0H14','63/2O2O/0H14')
    result=score_one(reference,baseline,target,span)
    assert result['status']=='scored' and result['match']['distance']==3
    assert '65/2020' not in result['window_text']
    # A perfect quotation elsewhere must never improve the intended damaged location.
    target='một số điều theo Luật số 63/2020/QH14; '+target
    other=score_one(reference,baseline,target,span)
    assert other['match']['distance']==3 and other['matched_span'][0]>50
    deleted=score_one('abc','prefix abc suffix','prefix  suffix',[7,10])
    assert deleted['status']=='scored' and deleted['match']['distance']==3 and deleted['confirmed_deletion']
    save(OUT/'anchored-scorer-contract-checks.json',{'passed':True,'duplicate_law_number_case':result,'perfect_decoy_case':other,'deleted_anchor_case':deleted})


def main():
    contracts()
    audit=json.loads((OUT/'baseline-anchor-audit.json').read_text())
    overrides_path=OUT/'anchor-overrides.json'
    overrides=json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    if isinstance(overrides,list):
        overrides={f'{r["page"]:02}-{r["index"]}':r | {'baseline_span':[r['start'],r['end']]} for r in overrides}
    if 'overrides' in overrides:
        overrides=overrides['overrides']
    anchored=[]
    for anchor in audit['anchors']:
        updated=dict(anchor)
        if updated['reference_id'] in overrides:
            override=overrides[updated['reference_id']]
            updated['baseline_span']=override['baseline_span']
            updated['status']='manual_verified'
            updated['override']=override
        anchored.append(updated)
    frozen={'anchors':anchored,'baseline_audit_sha256':hashlib.sha256((OUT/'baseline-anchor-audit.json').read_bytes()).hexdigest(),'overrides_sha256':hashlib.sha256(overrides_path.read_bytes()).hexdigest() if overrides_path.exists() else None,'window_padding_characters':8,'truth':'references.json unchanged; observed and mapped spans used only for location'}
    save(OUT/'anchored-references.json',frozen)
    raw_path=OUT/'raw-summary.unanchored.json'
    summary=json.loads((raw_path if raw_path.exists() else OUT/'superseded-unanchored/summary.json').read_text())
    configs=summary['configurations']
    review=[]
    scoring_override_path=OUT/'target-anchor-overrides.json'
    scoring_overrides=json.loads(scoring_override_path.read_text()) if scoring_override_path.exists() else []
    for override in scoring_overrides:
        if 'target_span' in override:
            left,right=override['target_span']
            padding=override.get('padding',0)
            override['window_span']=[max(0,left-padding),right+padding]
    scoring_overrides={(r['configuration'],r['reference_id']):r for r in scoring_overrides}
    for config in configs:
        matches=[]
        for p in range(1,37):
            baseline=norm((OUT/f'fast-144/page-{p:02}.txt').read_text())
            target=norm((OUT/config['configuration']/f'page-{p:02}.txt').read_text())
            for anchor in anchored:
                if anchor['page']!=p:
                    continue
                result={'reference_id':anchor['reference_id'],'page':p,'reference':anchor['reference']}
                if anchor['status'] not in ['unique_exact','manual_verified']:
                    result.update(status='unresolved',reason='baseline_anchor_unresolved')
                else:
                    result.update(score_one(anchor['reference'],baseline,target,anchor['baseline_span']))
                override=scoring_overrides.get((config['configuration'],anchor['reference_id']))
                if override:
                    if override.get('confirmed_deletion'):
                        length=len(norm(anchor['reference']))
                        result.update(status='scored',confirmed_deletion=True,window_boundary_contact=False,match={'distance':length,'reference_characters':length,'cer':1.0,'reference':norm(anchor['reference']),'matched_text':'','start':0,'end':0})
                    elif 'window_span' in override:
                        result['automatic_mapped_span']=result.get('mapped_span')
                        if 'target_span' in override:
                            result['mapped_span']=override['target_span']
                        result.pop('reason',None)
                        left,right=override['window_span']
                        matched=align(anchor['reference'],target[left:right])
                        result.update(status='scored',window_span=[left,right],window_text=target[left:right],matched_span=[left+matched['start'],left+matched['end']],match=matched,window_boundary_contact=(matched['start']==0 and left>0) or (matched['end']==right-left and right<len(target)))
                    result['pre_override_window_boundary_contact']=result.get('window_boundary_contact')
                    result['window_boundary_contact']=False
                    result['manual_scoring_override']=override
                matches.append(result)
        for result in matches:
            fraction=result.get('mapping',{}).get('equal_fraction',1)
            if result['status']!='scored' or result.get('window_boundary_contact') or fraction<0.8 or result.get('manual_scoring_override'):
                review.append({'configuration':config['configuration'],**result})
        save(OUT/config['configuration']/'anchored-matches.json',matches)
        scored=[m for m in matches if m['status']=='scored']
        config.update(matched_quotations=len(scored),reference_characters=sum(m['match']['reference_characters'] for m in scored),edit_distance=sum(m['match']['distance'] for m in scored),unresolved=[m['reference_id'] for m in matches if m['status']!='scored'],window_boundary_contacts=[m['reference_id'] for m in scored if m['window_boundary_contact']])
        eligible = config.get('status') == 'complete' and config.get('successful_pages') == 36
        config['fixed_quotation_sample_cer']=config['edit_distance']/config['reference_characters'] if eligible and len(scored)==108 else None
        config['partial_resolved_sample_cer']=config['edit_distance']/config['reference_characters'] if scored else None
    complete=[c for c in configs if c['fixed_quotation_sample_cer'] is not None]
    for c in configs:
        c['pareto_nondominated']=bool(c in complete and not any(o['total_seconds']<=c['total_seconds'] and o['fixed_quotation_sample_cer']<=c['fixed_quotation_sample_cer'] and (o['total_seconds']<c['total_seconds'] or o['fixed_quotation_sample_cer']<c['fixed_quotation_sample_cer']) for o in complete))
    summary['limitations']=['108 previously selected visual quotations are not random and may favor previously inspected outputs; not full-text CER', 'Validated baseline observed OCR is used only to locate each quotation; frozen image references remain the truth', 'Whole-page monotone text alignment maps each fixed baseline span to the target; scoring searches only mapped span +/-8 normalized characters; target OCR reordering and similar paragraphs can misalign and require review', 'Three exceptional target mappings were manually reviewed with explicit exact spans or confirmed deletion; audit and override hashes retained', 'Pareto membership is observed single-run result over twelve measured configurations, not statistical certainty or global optimum; original eight and added four ran in separate non-interleaved periods']
    summary.update(configurations=configs,metric='anchored fixed quotation sample CER; normalized whole-page SequenceMatcher mapping then local +/-8 character window semi-global Levenshtein; not full-text CER',anchors_sha256=hashlib.sha256((OUT/'anchored-references.json').read_bytes()).hexdigest(),target_overrides_sha256=hashlib.sha256(scoring_override_path.read_bytes()).hexdigest() if scoring_override_path.exists() else None)
    save(OUT/'mapping-review.json',{'threshold_equal_fraction':0.8,'count':len(review),'unresolved_count':sum(r['status']!='scored' for r in review),'boundary_contact_count':sum(bool(r.get('window_boundary_contact')) for r in review),'cases':sorted(review,key=lambda r:r.get('mapping',{}).get('equal_fraction',1))})
    save(OUT/'summary.json',summary)
    lines=['# 越南 PDF Tesseract 基准：定位约束评分','','冻结108条原图引文；观察文本只用于定位，不作真值。先映射已核对基线区间，再在映射区间±8字符窗口计算最小编辑距离。全页任意位置匹配结果已归入 superseded-unanchored，不能用作推荐。','','|配置|总耗时秒|已评分引文|编辑距离|固定引文样本CER|非支配|','|---|---:|---:|---:|---:|---|']
    for c in configs:
        cer=c['fixed_quotation_sample_cer']
        lines.append(f'|{c["configuration"]}|{c["total_seconds"]:.3f}|{c["matched_quotations"]}|{c["edit_distance"]}|{cer:.6%}|{c["pareto_nondominated"]}|' if cer is not None else f'|{c["configuration"]}|{c["total_seconds"]:.3f}|{c["matched_quotations"]}|{c["edit_distance"]}|UNRESOLVED|待审核|')
    lines+=['','复现顺序：先计时并输出 raw-summary.unanchored.json（不可直接推荐），再定位审计，最后执行定位评分生成最终summary.json/report.md。计时命令：`uv run --no-sync python tests/tesseract_frontier_benchmark.py --resume`。定位审计：`uv run --no-sync python tests/tesseract_frontier_anchor_audit.py`。定位评分：`uv run --no-sync python tests/tesseract_frontier_anchored_score.py`。','计时逐页重新渲染并编码PNG，OCR包含进程启动；OEM1 PSM3 vie、显式--dpi，OMP_THREAD_LIMIT=1。评分在全部计时后执行。前8配置与后4配置分两时段串行，未交错且无重复试验，系统负载与热漂移未受控。','','定位契约含相似63/65法规段落、完美异位诱饵与目标删除场景；详见anchored-scorer-contract-checks.json。逐页page-xx.json中的unanchored_matches_superseded是已废弃的无锚定评分，不可用作推荐；计时仍有效。逐条定位、映射区间、窗口和匹配区间见各配置anchored-matches.json；无法定位保留unresolved。','','局限：固定引文来自此前目视检查，非随机样本；不代表全文CER。全文单调文本映射不等于图像坐标定位，必须审查低匹配比例与窗口边界接触案例。单次时间与样本准确性得到的是本次网格的实测非支配点，不保证全局最优。']
    lines+=['','首轮全页任意位置最优匹配已废弃，superseded-unanchored仅作历史审计；冻结references.json中的旧方法描述属历史信息，最终方法与边界以summary.json及anchored-references.json为准。基线observed只用于定位，原图reference才是真值。']
    (OUT/'report.md').write_text('\n'.join(lines)+'\n')
    print([(c['configuration'],c['fixed_quotation_sample_cer'],c['unresolved']) for c in configs])

if __name__=='__main__':
    main()
