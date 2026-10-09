"""Offline research acceptance: retained evidence, map coverage and inspected samples."""
import hashlib
import json
import socket

from probe import MAP, SESSION, inventory, save
from report import HERE, resolve_body
from scrapy.http import HtmlResponse


def main():
    def no_network(*args, **kwargs):
        raise RuntimeError('offline_network_forbidden')

    socket.create_connection = no_network
    socket.socket.connect = no_network
    socket.getaddrinfo = no_network
    checks = []

    def check(name, actual, expected=True):
        checks.append({'name': name, 'expected': expected, 'actual': actual, 'status': 'PASS' if actual == expected else 'FAIL'})

    base = HERE / 'results/20261009'
    extra = HERE / 'results/20261009-extra'
    results = json.loads((base/'results.json').read_text())
    lookup = {key: result for result in results for key in result['ids']}
    rows = json.loads((HERE/'results/classification/entries.json').read_text())
    check('all_491_map_rows', [r['id'] for r in rows], [r['id'] for r in inventory()])
    check('exactly_491_rows', len(rows), 491)
    check('unique_row_ids', len({r['id'] for r in rows}), 491)
    check('all_classified', all(r['class'] in 'ABCDEFGX' for r in rows))
    check('footer_noise_preserved', [r['id'] for r in rows if r['class']=='X'], ['dd-489','dd-490','dd-491'])
    check('not_all_A_claimed_reviewed', all('自动识别' in r['evidence'] for r in rows if r['class']=='A' and r['id'] not in ('dd-017','dd-041','dd-116','dd-102')))
    manifest = json.loads((base/'manifest.json').read_text())
    check('map_sha256', hashlib.sha256(MAP.read_bytes()).hexdigest(), manifest['map_sha256'])
    request_count = 0
    for campaign in sorted((HERE/'results').iterdir()):
        if not (campaign/'requests.jsonl').exists():
            continue
        ledger = [json.loads(line) for line in (campaign/'requests.jsonl').read_text().splitlines()]
        request_count += len(ledger)
        check('ledger_terminal:'+campaign.name, all('status' in r or 'error' in r for r in ledger))
        for path in (campaign/'bodies').iterdir():
            body = path.read_bytes()
            check('hash:'+campaign.name+':'+path.name, hashlib.sha256(body).hexdigest(), path.name)
            check('no_session:'+campaign.name+':'+path.name, bool(SESSION.search(body)), False)
        redactions = json.loads((campaign/'redactions.json').read_text()) if (campaign/'redactions.json').exists() else []
        for r in redactions:
            check('redaction_removed:'+r['original_sha256'], (campaign/r['original_path']).exists(), False)
            check('sanitized_hash:'+r['original_sha256'], hashlib.sha256((campaign/r['sanitized_path']).read_bytes()).hexdigest(),r['sanitized_sha256'])
    check('bounded_request_count',request_count < 1500)

    def response(root,record):
        file,_=resolve_body(root,record)
        return HtmlResponse(record['url'],body=file.read_bytes(),headers={'Content-Type':record['mime']})

    samples = [
        ('dd-017','.txt_txt','指导性案例279号','计算机软件著作权'),
        ('dd-041','#fontzoom','第六十三批指导性案例','陈某抢劫再审抗诉案'),
        ('dd-116','#zoom','海市监列严〔2026〕007003号','海南数资跃动投资有限公司'),
    ]
    extracted = []
    for key,selector,title,fact in samples:
        result=lookup[key]
        e,d=result['entry'],result['detail']
        page=response(base,d)
        body=' '.join(page.css(selector).xpath('.//text()').getall()).strip()
        check(key+':list_links_to_detail',any(a['url']==d['url'] for a in e['links']))
        check(key+':title',title in d['title'])
        check(key+':body_fact',fact in body)
        check(key+':substantial_body',len(body)>500)
        extracted.append({'id':key,'title':d['title'],'source_url':d['url'],'selector':selector,'body_chars':len(body),'expected_fact':fact,'raw_sha256':d['sha256']})
    check('court_page2_observed',lookup['dd-017']['next_page']['outcome'],'ACCESSIBLE_HTML')
    check('court_page2_differs',lookup['dd-017']['next_page']['sha256']!=lookup['dd-017']['entry']['sha256'])
    more=json.loads((HERE/'results/20261009-page-confirm/results.json').read_text())
    for name,key in [('spp_page2','dd-041'),('haikou_page2','dd-116')]:
        check(name+':html',more[name]['outcome'],'ACCESSIBLE_HTML')
        check(name+':different_links',more[name]['links']!=lookup[key]['entry']['links'])
    extras=json.loads((extra/'results.json').read_text())
    pages=json.loads((HERE/'results/20261009-pagination/results.json').read_text())
    for name,field,value in [('amac_managers','houseName','国泰基金管理有限公司'),('amac_sales','orgName','中国建设银行'),('amac_trustees','trustName','中国工商银行股份有限公司')]:
        record=extras[name]
        file,_=resolve_body(extra,record)
        obj=json.loads(file.read_text())
        check(name+':business_code',obj['data']['errcode'],0)
        records=obj['data']['data']['dataList']
        check(name+':ten_records',len(records),10)
        check(name+':first_business_value',records[0][field],value)
    file,_=resolve_body(HERE/'results/20261009-pagination',pages['amac_managers_page2'])
    obj=json.loads(file.read_text())
    check('amac_page2_first','易方达基金管理有限公司',obj['data']['data']['dataList'][0]['houseName'])
    check('amac_page2_ten',len(obj['data']['data']['dataList']),10)
    company=response(extra,extras['companies_query'])
    check('companies_query_real_result',bool(company.css('a[href^="/company/"]')))
    safe=response(extra,extras['safe_iframe'])
    check('safe_default_list_20',len(safe.css('a[href*="listQuery?saffno="]')),20)
    check('safe_default_public_company','中国建设银行股份有限公司' in safe.text)
    check('safe_page2_received',pages['safe_page2']['outcome'],'ACCESSIBLE_HTML')
    check('safe_page2_different',pages['safe_page2']['sha256']!=extras['safe_iframe']['sha256'])
    check('reported_count_sums',sum(json.loads((HERE/'results/classification/summary.json').read_text())['classification'].values()),491)
    save(HERE/'results/classification/selected-checks.json',{'scope':'sampled public list/detail/pagination and parameter APIs; no claim of full site or Runtime acceptance','samples':extracted,'checks':[c for c in checks if not c['name'].startswith(('hash:','no_session:'))]})
    failed=[c for c in checks if c['status']=='FAIL']
    report={'scope':'offline research artifact integrity and manually inspected content samples; not production business acceptance','assertions':len(checks),'passed':len(checks)-len(failed),'failed':len(failed),'requests':request_count,'checks':checks}
    save(HERE/'results/classification/verification.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='checks'},ensure_ascii=False))
    return bool(failed)


if __name__=='__main__':
    raise SystemExit(main())
