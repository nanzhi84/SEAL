"""Rebuild the complete evidence-tiered source classification without network."""
import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from probe import ROOT, Probe, inventory, save
from scrapy.http import HtmlResponse

HERE = Path(__file__).resolve().parent
LABELS = {'A': '静态正文或名单样本', 'B': '静态内容链接，待验详情', 'C': '公开参数查询/API已取样', 'D': '输入/登录型入口', 'E': '动态页/API/iframe待适配', 'F': '失败/限制，机制未确认', 'G': '入口可读，内容未定位', 'X': '地图页脚噪声'}
PRIORITY = {'A': 'P1', 'B': 'P2', 'C': 'P1', 'D': '暂缓', 'E': 'P2', 'F': '待复查', 'G': 'P2', 'X': '排除'}
NEXT = {'A': '选定业务栏目，补分页/增量/正文准确性验收', 'B': '限定目标栏目并核对一篇真实正文/附件', 'C': '固定参数与分页合同，接入JSON/HTML Recipe', 'D': '先明确查询主体与必填项；账号/验证码保留人工入口', 'E': '检查业务JS与网络请求，优先公开API而非浏览器', 'F': '复核入口迁移与当前环境；不得解释为无相关记录', 'G': '寻找新闻公告栏目；首页可读不等于业务数据可用', 'X': '移出业务接入清单，保留来源行映射'}


def resolve_body(root, record):
    path = record.get('raw_path')
    if not path:
        return None, None
    redactions = json.loads((root / 'redactions.json').read_text()) if (root / 'redactions.json').exists() else []
    redacted = next((r for r in redactions if r['original_path'] == path), None)
    actual = root / (redacted['sanitized_path'] if redacted else path)
    expected = redacted['sanitized_sha256'] if redacted else record['sha256']
    if not actual.exists() or hashlib.sha256(actual.read_bytes()).hexdigest() != expected:
        raise ValueError('body_integrity:' + str(actual))
    return actual, 'sanitized' if redacted else 'raw'


def enrich(probe, root, record):
    result = dict(record)
    path, tier = resolve_body(root, record)
    if path:
        result['body_evidence'] = str(path.relative_to(ROOT))
        result['body_tier'] = tier
    if path and record.get('outcome') in ('ACCESSIBLE_HTML', 'SKIP_INPUT', 'JS_SHELL'):
        response = HtmlResponse(record['url'], body=path.read_bytes(), headers={'Content-Type': record.get('mime', '')})
        result.update(probe.inspect(response))
        result['templates'] = len(re.findall(r'\{\{[^}]+\}\}|ng-repeat=|v-for=', response.text))
        result['body_evidence'] = str(path.relative_to(ROOT))
        result['body_tier'] = tier
        result['captcha_hint'] = bool(re.search(r'验证码|驗證碼|captcha|validatecode', response.text, re.I))
        # A reusable article container is evidence only when its text is substantial.
        result['article_blocks'] = [b for b in result.get('article_blocks', []) if b['chars'] >= 200]
    return result


def classify(row, result, extra):
    e, d = result['entry'], result.get('detail', {})
    outcome = e['outcome']
    if row['map_noise']:
        code = 'X'
    elif outcome not in ('ACCESSIBLE_HTML', 'ACCESSIBLE_PDF', 'ACCESSIBLE_JSON', 'SKIP_INPUT', 'JS_SHELL'):
        code = 'F'
    elif outcome == 'ACCESSIBLE_PDF' or e.get('article_blocks') or (d.get('outcome') == 'ACCESSIBLE_HTML' and d.get('article_blocks')) or d.get('outcome') == 'ACCESSIBLE_PDF':
        code = 'A'
    elif outcome == 'JS_SHELL' or e.get('templates', 0) > 2 or e.get('iframes'):
        code = 'E'
    elif outcome == 'SKIP_INPUT':
        code = 'D'
    elif e.get('candidates'):
        code = 'B'
    else:
        code = 'G'
    sample = d if d.get('article_blocks') or d.get('outcome') == 'ACCESSIBLE_PDF' else e
    forms = e.get('forms', [])
    fields = [f"{f['method']} {f['action']} [" + ', '.join(x['name'] or x['hint'] or '(无name)' for x in f['fields']) + ']' for f in forms if f['fields']]
    params = '; '.join(fields) or ('；'.join(x.get('name') or x.get('placeholder') or '(无name)' for x in e.get('inputs', []))) or '未识别；不推断必填参数'
    scope = '仅入口及最多一篇站内样本；不代表原清单所指核验业务已可用'
    evidence = e.get('reason') or e.get('error') or outcome
    if d:
        evidence += '；详情=' + d.get('outcome', 'UNKNOWN')
    if sample.get('article_blocks'):
        evidence += '；正文容器=' + ','.join(b['selector'] for b in sample['article_blocks'])
    if e.get('captcha_hint'):
        evidence += '；源码含验证码线索（不等于所有公开栏目受限）'
    mechanism = {'A': '无需主体输入的HTML/PDF内容样本', 'B': 'HTML有候选链接；详情正文尚未确认', 'C': '公开GET/参数化接口', 'D': '表单或登录；业务结果未提交验证', 'E': 'JS模板/iframe/API；不得把导航文本当数据', 'F': 'UNKNOWN：本轮取不到有效内容', 'G': 'HTML可读；只有入口级证据', 'X': '原材料页脚，非业务来源'}[code]
    overrides = {
        'dd-044': ('E', '听证入口只识别到页码输入，不能认定需要主体信息；列表加载机制待查', '前往=分页控件；业务查询参数未确认', None),
        'dd-047': ('D', '立案介绍及规则PDF可读；尚未进入真实立案操作，不能当作公开案件数据库', '立案所需案件材料/身份信息未实际验证', None),
        'dd-057': ('G', '页面可读但仅识别到站内search输入；不据此判定仲裁新闻需要登录', 'search为站内搜索；无已验证业务查询参数', None),
        'dd-077': ('B', '首页article标签包含卡片摘要，不能当作完整文章正文；详情需单独适配', '公开新闻候选无需主体输入；其他查询未验', None),
        'dd-140': ('G', '首页article标签属于页面布局，未验证专利查询或新闻详情', '专利业务查询参数未确认；官网新闻需定位栏目', None),
        'dd-192': ('G', '当前只识别到站内搜索/链接控件，未确认目标业务数据', '站内关键字；不能推断业务必填项', None),
        'dd-429': ('D', '实际取得航空旅行常识文章，不是行程单验真结果；验真入口有验证码线索', '行程单业务字段尚未逐项验证；未输入旅客信息', None),
        'dd-009': ('C', 'GET /search?q=TESCO 返回企业结果列表；非精确主体匹配验收', 'q=企业名称/编号；page为候选分页参数，未验', 'companies_query'),
        'dd-100': ('E', 'Angular模板；已读index.js，global.getCDN按itemId/pageSize取DocInfo；数据端点未验证', 'itemId、pageSize、orderBy；详情docId（来自源码）', None),
        'dd-102': ('A', 'iframe默认表格20条；直接GET可读，page=2也取得；筛选含验证码线索', 'siteid=beijing、page；筛选POST irregularityno=统一社会信用代码/组织机构代码，未提交', 'safe_iframe'),
        'dd-247': ('C', '公募基金管理人GET JSON第一页10条，第二页10条，页面源码与响应一致', 'pageNo,pageSize,houseName,registerAddr,officeAddr；后三项可空', 'amac_managers'),
        'dd-248': ('C', '公募基金销售机构GET JSON第一页10条；全量分页未验', 'pageNo,pageSize,orgName,regAddr,orgType,startTime,endTime；筛选可空', 'amac_sales'),
        'dd-250': ('C', '基金托管机构GET JSON第一页10条；全量分页未验', 'pageNo,pageSize', 'amac_trustees'),
        'dd-484': ('B', '默认合同列表无需输入；详情本轮超时。表单筛选有验证码；已有独立列表样本基线', 'POST searchContractCode,searchContractName,searchProjCode,searchProjName,searchPurchaserName,searchSupplyName,searchAgentName,searchPlacardStartDate,searchPlacardEndDate,code', None),
        'dd-357': ('B', '默认核发许可证表格已有独立基线；本轮取得详情HTML，但字段/分页尚未验收', '按企业/地区等筛选需进一步核对；不能把首页15条当全国集合', None),
        'dd-004': ('B', '公开公告与PDF链接；部分列表JS加载。中文PDF路径初次本地编码失败，修正后HTTP200；PDF正文未验收', '静态公告无需输入；type为栏目枚举；企业核验跳转GSXT', None),
    }
    if row['id'] in overrides:
        code, mechanism, params, key = overrides[row['id']]
        evidence = mechanism
        if key and key in extra:
            sample = extra[key]
        scope = '仅以上明确描述的公开内容与参数；未验证全量、主体完整核验或长期稳定性'
    if code == 'A' and row['id'] not in ('dd-017', 'dd-041', 'dd-116', 'dd-102'):
        evidence += '；自动识别的正文样本，语义准确性待逐源核对'
    if row['id'] in ('dd-017', 'dd-041', 'dd-116'):
        scope = '人工读取的样本正文；字段断言见selected-checks.json；历史全量尚未验收'
        params = {'dd-017': '无需主体输入；下一页77_2.html；content仅站内搜索', 'dd-041': '无需主体输入；下一页index_2.shtml；qt仅站内搜索', 'dd-116': '无需主体输入；下一页index_1.shtml；title仅站内搜索'}[row['id']]
    elif row['id'] not in overrides and (fields or e.get('inputs')):
        params = '页面字段线索，必填性未验：' + params
    priority = 'P0' if row['id'] in ('dd-017', 'dd-041', 'dd-116') else PRIORITY[code]
    return {**{k: row[k] for k in ('id', 'line', 'category', 'section', 'name', 'map_url')}, 'host': urlsplit(row['url']).hostname, 'class': code, 'classification': LABELS[code], 'priority': priority, 'access': outcome, 'http_status': e.get('status'), 'mechanism': mechanism, 'parameters': params, 'evidence': evidence, 'scope': scope, 'next_step': NEXT[code], 'entry_url': e.get('url', row['url']), 'sample_url': sample.get('url', ''), 'candidate_url': result.get('detail_link', {}).get('url', ''), 'next_page': result.get('next_page', {}).get('outcome', '未请求'), 'observed_at': e.get('at', ''), 'body_evidence': sample.get('body_evidence', ''), 'forms': forms, 'api_hints': e.get('api_hints', []), 'iframes': e.get('iframes', [])}


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaign', type=Path, default=HERE/'results/20261009')
    parser.add_argument('--extra', type=Path, default=HERE/'results/20261009-extra')
    parser.add_argument('--output', type=Path, default=HERE/'results/classification')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    probe = Probe(args.campaign)
    results = json.loads((args.campaign/'results.json').read_text())
    extra = {k: enrich(probe,args.extra,v) for k,v in json.loads((args.extra/'results.json').read_text()).items()}
    lookup = {}
    for result in results:
        for key in ('entry','detail','next_page'):
            if key in result:
                result[key] = enrich(probe,args.campaign,result[key])
        for key in result['ids']:
            lookup[key] = result
    pages_root = HERE/'results/20261009-page-confirm'
    if args.campaign.resolve() == (HERE/'results/20261009').resolve() and (pages_root/'results.json').exists():
        pages = json.loads((pages_root/'results.json').read_text())
        for name, key in [('spp_page2','dd-041'),('haikou_page2','dd-116')]:
            lookup[key]['next_page'] = enrich(probe,pages_root,pages[name])
    source_rows = inventory()
    if set(lookup) != {r['id'] for r in source_rows}:
        raise ValueError('map_coverage_mismatch')
    rows = [classify(row,lookup[row['id']],extra) for row in source_rows]
    counts = dict(Counter(r['class'] for r in rows))
    save(args.output/'entries.json',rows)
    write_csv(args.output/'entries.csv',rows)
    hosts = defaultdict(list)
    for row in rows:
        hosts[row['host']].append(row)
    write_csv(args.output/'hosts.csv',[{'host': h,'entry_count':len(rs),'classes':','.join(sorted({r['class'] for r in rs})),'ids':','.join(r['id'] for r in rs),'names':'；'.join(dict.fromkeys(r['name'] for r in rs))} for h,rs in sorted(hosts.items())])
    summary = {'rows':len(rows),'normalized_urls':len({r['url'] for r in source_rows}),'hosts':len(hosts),'classification':counts,'by_category':{c:dict(Counter(r['class'] for r in rows if r['category']==c)) for c in dict.fromkeys(r['category'] for r in rows)},'scope':'A is observed body-container evidence except manually checked P0; not full business acceptance'}
    save(args.output/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False))


if __name__ == '__main__':
    main()
