#!/usr/bin/env python3
"""Offline, independently labelled same-URL content audit. No network dispatch.

Quality of returned snapshots is separate from unverifiable cloud cache age.
Every blocked/failed case remains in the matrix. The frozen Recipe is never
repaired to improve this run's results. Native JSON is compared to the actual
Scrapy structured Recipe, not to a text parser artificially scored as JSON.
"""
import argparse
import json
import re
import socket
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag

from parsel import Selector
from bench.common import HERE, read_json, write_json, sha256_hex
from bench.extract import extract_generic
from bench.recipes import apply
from bench.revision import content_hash


def compact(text):
    return re.sub(r'\s+', '', str(text or ''))


class InspectHTML(HTMLParser):
    """Independent semantic evidence parser; no Recipe/CSS/XPath calls."""
    def __init__(self, raw, url):
        super().__init__(convert_charrefs=True)
        self.url, self.parts, self.frames, self.forms, self.links = url, [], [], [], []
        self.skip, self.form = 0, None
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.skip += 1
        if tag == 'iframe' and attrs.get('src'):
            self.frames.append(urljoin(self.url, attrs['src']))
        if tag == 'a' and attrs.get('href'):
            self.links.append(urljoin(self.url, attrs['href']))
        if tag == 'form':
            self.form = {'action': urljoin(self.url, attrs.get('action', '')),
                         'method': attrs.get('method', 'get').lower(), 'names': []}
            self.forms.append(self.form)
        if tag == 'input' and self.form is not None and attrs.get('name'):
            self.form['names'].append(attrs['name'])

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip-1)
        if tag == 'form':
            self.form = None

    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)

    @property
    def text(self):
        return ' '.join(self.parts)


def adapter(case, html):
    """Shared small SEAL adapter for form/iframe contracts, not native magic."""
    tree = Selector(text=html)
    if case['function_id'] == 'search_interface':
        form = tree.css('form[role="search"]')
        return {'forms': [{'form_action': urljoin(case['url'], f.attrib.get('action', '')),
                           'query_parameters': f.css('input::attr(name)').getall(),
                           'method': f.attrib.get('method', 'get').lower()} for f in form]}
    return {'iframe_target_urls': [urljoin(case['url'], u) for u in tree.css('iframe::attr(src)').getall()]}


def outputs(case, engine, observation, root):
    raw_path = root / observation['raw_path']
    raw = raw_path.read_text(encoding=observation.get('encoding') or 'utf-8', errors='replace')
    snapshot = f'{engine}:{case["id"]}:{sha256_hex(raw_path.read_bytes())[:12]}'
    function = case['function_id']
    started = time.perf_counter()
    if engine == 'firecrawl_cloud':
        native = read_json(root / observation['document_path'])
        material = native.get('html') or ''
        meta = native.get('metadata') or {}
        general = {'title': meta.get('title'), 'published_at': meta.get('published_time'),
                   'body_text': InspectHTML(material, case['url']).text,
                   'structured': native.get('json'), 'links': native.get('links') or [],
                   'implementation': 'Firecrawl native HTML/JSON; HTML-to-text representation only'}
    else:
        material = raw
        general = {**extract_generic(raw, case['url']),
                   'implementation': 'Scrapy Response + trafilatura; no structured schema output'}
    if function in ('search_interface', 'iframe_discovery'):
        general.update(adapter(case, material))
        general['contract_adapter'] = 'shared SEAL HTML attribute adapter, not a native framework feature'
    recipe_name = {'company_fields': 'ch_company', 'iframe_discovery': 'stats_iframe',
                   'iframe_content': 'stats_page'}.get(function)
    if recipe_name:
        recipe = apply(recipe_name, raw, case['url'], snapshot)
        replay = apply(recipe_name, raw, case['url'], snapshot)
        deterministic = recipe == replay
        if function == 'iframe_discovery':
            recipe['iframe_target_urls'] = [f['url'] for f in recipe['iframes']]
    else:
        recipe = adapter(case, raw)
        deterministic = recipe == adapter(case, raw)
    out = {'minimal_content': general, 'recipe_controlled': recipe,
           'raw_path': observation['raw_path'], 'raw_sha256': sha256_hex(raw_path.read_bytes()),
           'offline_parse_seconds': time.perf_counter()-started,
           'recipe_replay_deterministic': deterministic,
           'recipe_content_hash': content_hash(recipe) if recipe_name else None}
    folder = root / 'parsed' / engine
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / (case['id']+'.json'), out)
    return out, InspectHTML(raw, case['url'])


def evidence_valid(raw, doc):
    tree = Selector(text=raw)
    matches = []
    for field, ev in doc.get('_evidence', {}).items():
        node = tree.xpath(ev['xpath']) if ev else []
        quote = ev.get('quote', '') if ev else ''
        matches.append({'field': field, 'resolves': bool(node) and
                        compact(quote) in compact(node[0].xpath('string(.)').get())})
    return matches


def grade(case, output, oracle, raw_inspection):
    result = {}
    fid = case['function_id']
    for scenario in ('minimal_content', 'recipe_controlled'):
        doc = output[scenario]
        if fid == 'company_fields':
            fields = doc.get('structured')
            result[scenario] = {'structured_format_available': isinstance(fields, dict),
                'fields': {k: compact(fields.get(k)) == compact(v) for k,v in oracle.items()} if isinstance(fields, dict) else None,
                'expected': len(oracle),
                'matched': sum(compact((fields or {}).get(k)) == compact(v) for k,v in oracle.items())
                           if isinstance(fields, dict) else None}
        elif fid == 'search_interface':
            forms = doc.get('forms', [])
            result[scenario] = {'form_action': any(f['form_action'] == oracle['form_action'] for f in forms),
                                'query_parameter': any(oracle['query_parameter'] in f['query_parameters'] for f in forms),
                                'method_get': any(f['method'] == 'get' for f in forms),
                                'search_traversal': 'NOT_TESTED'}
        elif fid == 'iframe_discovery':
            found = doc.get('iframe_target_urls', [])
            result[scenario] = {'expected': len(oracle['iframe_target_urls']), 'found': found,
                                'matched': len(set(found) & set(oracle['iframe_target_urls']))}
        elif fid == 'iframe_content':
            body = compact(doc.get('body_text'))
            rows = oracle['rows']
            positions = [body.find(compact(name+date)) for name,date,_ in rows]
            result[scenario] = {'expected_rows': len(rows), 'row_name_date_matches': sum(i>=0 for i in positions),
                                'ordered': all(i>=0 for i in positions) and positions == sorted(positions),
                                'title_matches_html_head': doc.get('title') == oracle['html_title'],
                                'business_title_extracted': doc.get('title') == oracle['business_title'],
                                'body_chars': len(body),
                                'core_rows_only_exact': body == compact(''.join(n+d for n,d,_ in rows))}
    if fid == 'company_fields':
        raw_text = compact(raw_inspection.text)
        result['oracle_verified_in_raw'] = {k: all(compact(x) in raw_text for x in v.split('|'))
                                            for k,v in oracle.items()}
    elif fid == 'iframe_content':
        raw_text = compact(raw_inspection.text)
        result['oracle_verified_in_raw'] = [compact(n+d) in raw_text and u in raw_inspection.links
                                            for n,d,u in oracle['rows']]
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('campaign')
    root = HERE / ap.parse_args().campaign
    def deny_network(*_args, **_kwargs):
        raise RuntimeError('OFFLINE_AUDIT_NETWORK_FORBIDDEN')
    socket.socket.connect = deny_network
    protocol = read_json(root / 'protocol.json')
    run, oracle = read_json(root / 'run.json'), read_json(root / 'oracle.json')
    for filename in ('bench/recipes.py', 'bench/extract.py'):
        if sha256_hex((HERE/filename).read_bytes()) != run['source_sha256'][filename]:
            raise RuntimeError('FROZEN_RECIPE_VERSION_MISMATCH; restore recorded source for replay')
    ledger = read_json(root / 'ledger.json')
    matrix, matched_pairs = [], []
    all_outputs = {}
    for case in protocol['cases']:
        row = {'case_id': case['id'], 'url': case['url'], 'function_id': case['function_id'],
               'engines': {}, 'same_url_successful_operations': False,
               'strict_freshness_verified': False, 'oracle_sha256': sha256_hex((root/'oracle.json').read_bytes())}
        inspections = {}
        for engine, ob in run['cases'][case['id']]['engines'].items():
            record = {'status': ob['status'], 'http_status': ob.get('http_status')}
            row['engines'][engine] = record
            if ob['status'] != 'FETCHED' or ob.get('http_status') != 200:
                continue
            if case['id'] not in oracle:
                record['quality'] = 'UNKNOWN_UNANNOTATED'
                continue
            output, inspected = outputs(case, engine, ob, root)
            inspections[engine] = inspected
            all_outputs[case['id'], engine] = output
            record['quality'] = grade(case, output, oracle[case['id']], inspected)
            record['recipe_replay_deterministic'] = output['recipe_replay_deterministic']
            record['snapshot_sha256'] = output['raw_sha256']
            record['recipe_content_hash'] = output['recipe_content_hash']
            if case['function_id'] == 'company_fields':
                raw = (root / ob['raw_path']).read_text()
                record['field_evidence'] = evidence_valid(raw, output['recipe_controlled'])
        if len(inspections) == 2:
            observations = run['cases'][case['id']]['engines']
            timestamps = [datetime.fromisoformat(o['started_at']).timestamp() for o in observations.values()]
            same_url = all(o['requested_url'] == case['url'] and o['final_url'] == case['url']
                           for o in observations.values())
            row.update(same_url_successful_operations=same_url, dispatch_gap_s=abs(timestamps[0]-timestamps[1]),
                       inside_300s_window=abs(timestamps[0]-timestamps[1])<=300,
                       cache_request='maxAge=0/storeInCache=false; Scrapy HTTP cache disabled',
                       cloud_cache_telemetry='UNKNOWN: cache_state and cached_at not supplied',
                       comparison_scope='returned-content quality; NOT proven cache-age or core-runtime benchmark')
            if case['function_id'] in ('company_fields','iframe_content'):
                row['oracle_matches_both_raws'] = all(all(
                    (q.values() if isinstance(q, dict) else q)) for q in
                    [r['quality']['oracle_verified_in_raw'] for r in row['engines'].values()])
            else:
                expected = oracle[case['id']]['source_title']
                row['source_identity_anchor_in_both'] = all(expected in i.text for i in inspections.values())
            row['snapshot_quality_pair_eligible'] = same_url and row['inside_300s_window'] and (
                row.get('oracle_matches_both_raws', row.get('source_identity_anchor_in_both', False)))
            if row['snapshot_quality_pair_eligible']:
                matched_pairs.append(case['id'])
        matrix.append(row)
    # Same external 9-field function: bespoke Scrapy Recipe vs native Firecrawl JSON.
    company = next(r for r in matrix if r['case_id'] == 'D_company')
    structured_task = {
        'scrapy': company['engines']['scrapy'].get('quality', {}).get('recipe_controlled'),
        'firecrawl_cloud': company['engines']['firecrawl_cloud'].get('quality', {}).get('minimal_content'),
        'implementation_difference': 'Scrapy uses source-specific Python Recipe; Firecrawl uses native JSON schema (+4 credits). Neither is compared against a text-only output.',
        'generic_scrapy_structured_score': 'NOT_APPLICABLE_TEXT_ONLY; no artificial 0/9 framework score'}
    parent = next(r for r in matrix if r['case_id'] == 'E_shell')
    inline = {}
    for engine in protocol['engines']:
        output = all_outputs.get(('E_shell',engine))
        if output:
            ob = run['cases']['E_shell']['engines'][engine]
            body = compact(InspectHTML((root/ob['raw_path']).read_text(), parent['url']).text)
            inline[engine] = {'core_name_date_pairs_in_parent_raw': sum(compact(n+d) in body for n,d,_ in oracle['E_iframe']['rows']),
                              'original_iframe_tags_preserved': output['recipe_controlled'].get('iframe_target_urls')}
            if engine == 'firecrawl_cloud':
                links = output['minimal_content']['links']
                targets = oracle['E_shell']['iframe_target_urls']
                recovered = [u for u in targets if u in {urldefrag(link).url for link in links}]
                inline[engine]['native_links_resource_recall'] = {
                    'expected_original_iframe_resources': len(targets), 'recovered': recovered,
                    'normalization': 'remove fragment only; never guess directory == index.html',
                    'supporting_native_links': [u for u in links if urldefrag(u).url in recovered],
                    'not_a_claim_of_preserved_iframe_structure': True}
    result = {
        'campaign_id': run['campaign_id'], 'status': 'PARTIAL_PAIRED_URL_CONTENT_EVIDENCE',
        'matrix': matrix, 'frozen_candidate_urls': len(protocol['cases']),
        'same_url_successful_operation_pairs': sum(r['same_url_successful_operations'] for r in matrix),
        'snapshot_quality_pairs': matched_pairs, 'strict_freshness_verified_pairs': 0,
        'structured_task': structured_task, 'parent_iframe_diagnostic': inline,
        'credits_actual_balance_delta': run['credits_balance_delta'],
        'client_requests': len(ledger['requests']),
        'requests_by_engine': {e: sum(r['engine']==e for r in ledger['requests'])
                               for e in ('robots','scrapy','firecrawl_cloud')},
        'minimum_client_start_interval_s': min(
            datetime.fromisoformat(b['started_at']).timestamp()-datetime.fromisoformat(a['started_at']).timestamp()
            for a,b in zip(ledger['requests'],ledger['requests'][1:])),
        'field_evidence_scope': 'Shared Recipe XPath/quote resolves, not provider-native field citations',
        'discovery_scope': 'Fixed URL seeds. Parent targets verified offline, NOT a live discovery-driven scheduler or whole-site coverage.',
        'pdf_articles_revision_playwright': 'NO_COMPARABLE_LIVE_RESULT_IN_THIS_ROUND',
        'cache_limitation': 'Four current URL API/fetch pairs, four content-quality pairs, zero pairs with independent cloud freshness telemetry; do not promote to strict-live/performance ranking.',
        'framework_preference': None,
        'grader_sources_sha256': {filename: sha256_hex((HERE/filename).read_bytes()) for filename in
                                  ('paired_quality.py', 'bench/recipes.py', 'bench/extract.py', 'bench/revision.py')},
    }
    write_json(root/'quality.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('matrix','structured_task','parent_iframe_diagnostic')},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
