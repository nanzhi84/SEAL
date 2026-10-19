"""Historical 2026-10-16 acceptance pinned to the then-current 120/144 DPI.
Current speed-priority acceptance: tests/speed_ocr_acceptance.py.
Independent Source -> real PDF worker -> final CrawlResult acceptance.
Failures declared first in source_ocr_config_failure_boundaries.json.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import time
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import yaml
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
from pypdf import PdfReader, PdfWriter

from seal.config import load_file
from seal.pdf_runtime import PDFRuntime
from seal.sdk import InputScraper

ROOT = Path(__file__).resolve().parents[1]
NAME = "source-ocr-config-20261016"
OUT = ROOT / "artifacts" / NAME
WORK = ROOT / "work" / NAME
EXPECTED = {
    "chi_sim": (120, "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730"),
    "chi_tra": (144, "529c5b5797d64b126065cd55f2bb4c7fd7b15790798091b1ff259941a829330b"),
    "eng": (120, "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"),
    "vie": (144, "79df64caf7bcfb2a27df5042ecb6121e196eada34da774956995747636d5bfa1"),
}
CHECKS = []


def check(name, condition):
    CHECKS.append({"assertion": name, "status": "PASS" if condition else "FAIL"})
    if not condition:
        raise AssertionError(name)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


async def run():
    inventory = []
    for path in sorted((ROOT / "sources").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        source = load_file(path)
        lang = "vie" if source.id == "dd-492" else "chi_sim"
        check(source.id + " explicit language/DPI", raw.get("ocr", {}).get("languages") == lang
              and raw.get("ocr", {}).get("dpi") == EXPECTED[lang][0])
        inventory.append({"id": source.id, "path": str(path.relative_to(ROOT)),
                          "sha256": sha(path), "ocr": source.ocr.model_dump()})
    check("source IDs unique", len({s["id"] for s in inventory}) == len(inventory))
    dump(OUT / "source-inventory.json", inventory)
    models_output = subprocess.check_output(["tesseract", "--list-langs"], text=True)
    model_dir = Path(models_output.split('"')[1])
    models = {}
    for lang, (_, expected_hash) in EXPECTED.items():
        model = model_dir / (lang + ".traineddata")
        actual_hash = sha(model)
        check(lang + " model present", model.is_file())
        if expected_hash:
            check(lang + " pinned fast model hash", actual_hash == expected_hash)
        project_model = ROOT / "work/ocr-tessdata" / model.name
        check(lang + " project model identical", sha(project_model) == actual_hash)
        models[lang] = {"path": str(model), "sha256": actual_hash,
                        "project_path": str(project_model), "project_sha256": sha(project_model)}
    dump(OUT / "models.json", models)
    corpus = ROOT / "artifacts/tesseract-multilang-20261016"
    samples = {
        "chi_sim": (corpus / "chi_sim/input/public-scan-a.pdf", 2, ["企业", "中文"]),
        "chi_tra": (corpus / "chi_tra/input/tainan.pdf", 1, ["會議", "臺南"]),
        "eng": (corpus / "eng/input/nara-recommendation-2018.pdf", 1, ["memorandum", "recommendation"]),
        "vie": (ROOT / "artifacts/dd-492-local-ocr-20261015-194700/dd-492-01-original.pdf", 1, ["CỘNG", "Việt"]),
    }
    for lang, (original, page_number, anchors) in samples.items():
        if lang in ("chi_sim", "vie"):
            source_path = ROOT / "sources" / ("dd-492.yaml" if lang == "vie" else inventory[0]["id"] + ".yaml")
        else:
            source_path = WORK / (lang + ".yaml")
            value = yaml.safe_load((ROOT / "sources" / inventory[0]["id"]).with_suffix(".yaml").read_text())
            value.update(id="dd-998" if lang == "eng" else "dd-999",
                         ocr={"languages": lang, "dpi": EXPECTED[lang][0]})
            source_path.write_text(yaml.safe_dump(value, allow_unicode=True))
        source = load_file(source_path)
        (OUT / (lang + "-source.yaml")).write_bytes(source_path.read_bytes())
        reader = PdfReader(original)
        writer = PdfWriter()
        writer.add_page(reader.pages[page_number - 1])
        sample = WORK / (lang + ".pdf")
        writer.write(sample)
        check(lang + " real scanned page has no text", not PdfReader(sample).pages[0].extract_text().strip())
        state = SimpleNamespace(config=source.model_dump(), cancelled=False, started=time.monotonic())
        runtime = PDFRuntime(state)
        capture = {"body": sample.read_bytes(), "archive_ref": "isolated-acceptance:" + sha(sample),
                   "headers": {"content-type": "application/pdf"}}
        try:
            capture["pdf_result"] = await runtime.prepare(capture)
        finally:
            await runtime.close()
        url = source.entry_urls[0]
        adapter = SimpleNamespace(captures={url: capture})
        config = CrawlerRunConfig(scraping_strategy=InputScraper(adapter), word_count_threshold=0)
        crawler = AsyncWebCrawler()
        result = await crawler.aprocess_html(url, "", None, config, None, None, False)
        dump(OUT / (lang + "-crawlresult.json"), result.model_dump(mode="json"))
        pdf = result.metadata["pdf"]
        text = pdf["pages"][0]["text"]
        (OUT / (lang + "-text.txt")).write_text(text)
        check(lang + " successful final CrawlResult", result.success)
        check(lang + " OCR used", pdf["method"] == "ocr")
        check(lang + " exactly one page retained", [p["page_number"] for p in pdf["pages"]] == [1])
        check(lang + " effective OCR matches Source", pdf["ocr"]["config"] == source.ocr.model_dump())
        check(lang + " final text anchors", all(a.casefold() in "".join(text.split()).casefold() for a in anchors))
        check(lang + " HTML and Markdown populated", bool(result.cleaned_html.strip()) and bool(result.markdown.raw_markdown.strip()))
        dump(OUT / (lang + "-input.json"), {"original": str(original.relative_to(ROOT)),
             "original_sha256": sha(original), "original_page": page_number, "sample_sha256": sha(sample),
             "anchors": anchors, "model": models[lang]})


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    os.environ["SEAL_WORK_DIR"] = str(WORK)
    failure = None
    try:
        asyncio.run(run())
    except Exception as exc:
        failure = str(exc)
    report = {"status": "FAIL" if failure else "PASS", "failure": failure, "checks": CHECKS,
              "command": "uv run --no-sync python tests/source_ocr_config_acceptance.py",
              "prerequisites": "Python 3.12 project uv environment, Tesseract with four fast models in its effective tessdata path; retained public corpus PDFs referenced in input artifacts; no DB/S3/network required",
              "python_dependencies": {p: version(p) for p in ("crawl4ai", "pypdf", "pypdfium2")},
              "boundary_file": "tests/source_ocr_config_failure_boundaries.json",
              "unverified": ["network fetch, DB/S3 persistence", "full-page OCR accuracy and corpus generalization", "all Source live harvesting"]}
    dump(OUT / "report.json", report)
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(bool(failure))
