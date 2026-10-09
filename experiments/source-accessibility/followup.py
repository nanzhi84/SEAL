"""Replay the explicitly inspected public form/API examples, with bounded GETs."""
import argparse
import concurrent.futures
from pathlib import Path

from probe import Probe, now, save

URLS = {
    'spp_page2': 'https://www.spp.gov.cn/spp/jczdal/index_2.shtml',
    'haikou_page2': 'http://amr.haikou.gov.cn/xxgk/spgsxx/xzcfangsxx/index_1.shtml',
    'companies_query': 'https://find-and-update.company-information.service.gov.uk/search?q=TESCO',
    'safe_iframe': 'https://www.safe.gov.cn/www/illegal?siteid=beijing',
    'safe_page2': 'https://www.safe.gov.cn/www/illegal/index?page=2&siteid=beijing',
    'nfra_js': 'https://www.nfra.gov.cn/cn/js/index/index.js?v=20200108',
    'nfra_common': 'https://www.nfra.gov.cn/cn/js/common/Script.js?v=20200108',
    'cninfo_js': 'http://static.cninfo.com.cn/new/assets/js/index.js?v=20260826101926',
    'amac_common': 'https://res.amac.org.cn/resources/js/common-d.js?2.1.2=',
    'amac_managers': 'https://www.amac.org.cn/portal/front/mutualFund/findMutualFundHousePage?pageNo=1&pageSize=10&houseName=&registerAddr=&officeAddr=',
    'amac_managers_page2': 'https://www.amac.org.cn/portal/front/mutualFund/findMutualFundHousePage?pageNo=2&pageSize=10&houseName=&registerAddr=&officeAddr=',
    'amac_sales': 'https://www.amac.org.cn/portal/front/infopublic/fsAgencyAnno/findFsAgencyAnnos?pageNo=1&pageSize=10&orgName=&regAddr=&orgType=&startTime=&endTime=',
    'amac_trustees': 'https://www.amac.org.cn/portal/front/financial/fundTrustee/findFundTrusteesPage?pageNo=1&pageSize=10',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--keys', nargs='*')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'bodies').mkdir()
    probe = Probe(args.output)
    urls = {k: v for k, v in URLS.items() if not args.keys or k in args.keys}
    save(args.output / 'manifest.json', {'started_at': now(), 'urls': urls, 'scope': 'public GET; TESCO is an example search, not an exhaustive subject check; API paths/params observed in source HTML/JS'})
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(probe.request, u, 'mechanism'): k for k, u in urls.items()}
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = {'url': urls[key], 'outcome': 'LOCAL_ERROR', 'reason': type(exc).__name__}
            save(args.output / 'results.json', results)
            print(key, results[key]['outcome'], flush=True)


if __name__ == '__main__':
    main()
