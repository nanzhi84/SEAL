"""Public SDK behavior: batch processing must retain page identity and links.
Boundary: repeated batch calls must not drop, duplicate, reorder or cross page text.
Run: uv run --no-sync python tests/pdf_sdk_batch_acceptance.py
"""
import json
import sys
import time
from importlib.metadata import version
from pathlib import Path
from crawl4ai.processors.pdf import PDFContentScrapingStrategy
from crawl4ai.processors.pdf.processor import NaivePDFProcessorStrategy
from pdf_upgrade_acceptance import linked_pdf
from dd492_pdf_acceptance import ROOT, pdf, sha

out = ROOT / 'artifacts' / ('pdf-sdk-batch-' + version('crawl4ai') + '-' + time.strftime('%Y%m%d_%H%M%S'))
out.mkdir(parents=True)
work = ROOT / 'work' / out.name
work.mkdir(parents=True)
body = pdf(25)
path = work / 'pages.pdf'
path.write_bytes(body)
checks, evidence = [], []
for repeat in range(6):
    result = NaivePDFProcessorStrategy(extract_images=False, batch_size=4).process_batch(path)
    pages = [{'page': p.page_number, 'text': p.raw_text, 'links': p.links} for p in result.pages]
    expected = [(i, f'INDEPENDENT_PAGE_{i:03d}_COMPLETE_TEXT') for i in range(1, 26)]
    ok = [(p['page'], p['text'].strip()) for p in pages] == expected
    checks.append({'name': f'batch repeat {repeat + 1} exact page number and text', 'status': 'PASS' if ok else 'FAIL'})
    evidence.append(pages)
linked = work / 'linked.pdf'
linked.write_bytes(linked_pdf())
link_result = NaivePDFProcessorStrategy(extract_images=False).process(linked)
links = [{'page': p.page_number, 'links': p.links} for p in link_result.pages]
(out / 'pages.json').write_text(json.dumps(evidence, indent=2))
(out / 'links.json').write_text(json.dumps(links, indent=2))
scraping = PDFContentScrapingStrategy().scrap(str(linked.resolve()), '')
(out / 'scraping-result.json').write_text(scraping.model_dump_json())
summary = {'status': 'PASS' if all(c['status'] == 'PASS' for c in checks) else 'FAIL', 'checks': checks,
           'crawl4ai': version('crawl4ai'), 'fixture_sha256': sha(body), 'linked_fixture_sha256': sha(linked.read_bytes()), 'artifact': str(out),
           'command': 'uv run --no-sync python tests/pdf_sdk_batch_acceptance.py',
           'scope': 'Public SDK process_batch six repetitions of 25 pages; separate process URI annotation evidence; no SEAL CLI'}
(out / 'summary.json').write_text(json.dumps(summary, indent=2))
print(json.dumps(summary))
sys.exit(summary['status'] != 'PASS')
