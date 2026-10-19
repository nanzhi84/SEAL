"""Independent behavioral acceptance; failure boundaries declared before implementation."""
import asyncio
import hashlib
import json
import os
import subprocess
import shutil
import time
import urllib.request
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
NAME = os.environ.get("SPEED_OCR_RUN", "speed-ocr-20261019")
WORK, OUT = ROOT / "work" / NAME, ROOT / "artifacts" / NAME
CHECKS, INPUTS = [], []
MODELS = {"chi_sim": "a5fcb6f0db1e1d6d8522f39db4e848f05984669172e584e8d76b6b3141e1f730",
          "chi_tra": "529c5b5797d64b126065cd55f2bb4c7fd7b15790798091b1ff259941a829330b",
          "eng": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
          "vie": "79df64caf7bcfb2a27df5042ecb6121e196eada34da774956995747636d5bfa1"}

def digest(data):
    return hashlib.sha256(data).hexdigest()

def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))

def check(name, condition):
    CHECKS.append({"assertion": name, "status": "PASS" if condition else "FAIL"})
    if not condition:
        raise AssertionError(name)

async def run():
    sources = [load_file(p) for p in sorted((ROOT / "sources").glob("*.yaml"))]
    check("exactly 20 unique Sources", len(sources) == 20 and len({s.id for s in sources}) == 20)
    for source in sources:
        lang = "vie" if source.id == "dd-492" else "chi_sim"
        check(source.id + " unchanged language and explicit fast96", source.ocr.languages == lang and source.ocr.dpi == 96)
        raw = yaml.safe_load((ROOT / "sources" / (source.id + ".yaml")).read_text())
        check(source.id + " explicit DPI", raw["ocr"]["dpi"] == 96)
    dump(OUT / "sources.json", [{"id": s.id, "ocr": s.ocr.model_dump()} for s in sources])
    template = (ROOT / "examples/source.yaml").read_text()
    check("four-language template speed recommendations", all(lang + ": 96 DPI" in template for lang in ["chi_sim", "chi_tra", "eng", "vie"]))
    check("template actual DPI", yaml.safe_load(template)["ocr"]["dpi"] == 96)
    legacy = yaml.safe_load((ROOT / "sources/dd-492.yaml").read_text())
    legacy.pop("ocr")
    legacy_path = WORK / "legacy.yaml"
    legacy_path.write_text(yaml.safe_dump(legacy, allow_unicode=True))
    check("historical omitted OCR DPI remains144", load_file(legacy_path).ocr.dpi == 144)
    models_output = subprocess.check_output(["tesseract", "--list-langs"], text=True)
    model_dir = Path(models_output.split('"')[1])
    manifest = json.loads((ROOT / "experiments/paddle-ocr-pareto/config/documents.json").read_text())
    for lang, suffix, page, anchors in [("chi_sim", "chi_sim-public-scan-a.pdf", 2, ["企业", "中文"]),
                                        ("vie", "vie-140-vbhn-vpqh.signed.pdf", 1, ["CỘNG", "Việt"]),
                                        ("chi_tra", "chi_tra-tainan.pdf", 1, ["會議", "臺南"]),
                                        ("eng", "eng-nara-recommendation-2018.pdf", 1, ["memorandum", "recommendation"])]:
        check(lang + " pinned fast model", digest((model_dir / (lang + ".traineddata")).read_bytes()) == MODELS[lang])
        item = next(v for k, v in manifest.items() if k.endswith(suffix))
        original = WORK / suffix
        persistent = ROOT / "experiments/paddle-ocr-pareto/work/20261019/inputs" / suffix
        if persistent.exists() and digest(persistent.read_bytes()) == item["sha256"]:
            shutil.copyfile(persistent, original)
        if not original.exists():
            with urllib.request.urlopen(item["url"], timeout=120) as response:
                original.write_bytes(response.read())
        check(lang + " original SHA", digest(original.read_bytes()) == item["sha256"])
        reader, writer = PdfReader(original), PdfWriter()
        writer.add_page(reader.pages[page - 1])
        sample = WORK / (lang + "-page.pdf")
        writer.write(sample)
        check(lang + " real page has no native text", not (PdfReader(sample).pages[0].extract_text() or "").strip())
        source = next((s for s in sources if s.ocr.languages == lang), None)
        if source is None:
            sample_config = yaml.safe_load((ROOT / "examples/source.yaml").read_text())
            sample_config.update(archive_approved=True, ocr={"languages": lang, "dpi": 96})
            sample_path = WORK / (lang + "-template.yaml")
            sample_path.write_text(yaml.safe_dump(sample_config))
            source = load_file(sample_path)
        state = SimpleNamespace(config=source.model_dump(), cancelled=False, started=time.monotonic())
        runtime = PDFRuntime(state)
        capture = {"body": sample.read_bytes(), "archive_ref": digest(sample.read_bytes()), "headers": {"content-type": "application/pdf"}}
        started = time.monotonic()
        try:
            capture["pdf_result"] = await runtime.prepare(capture)
        finally:
            await runtime.close()
        url = source.entry_urls[0]
        config = CrawlerRunConfig(scraping_strategy=InputScraper(SimpleNamespace(captures={url: capture})), word_count_threshold=0)
        result = await AsyncWebCrawler().aprocess_html(url, "", None, config, None, None, False)
        dump(OUT / (lang + "-crawlresult.json"), result.model_dump(mode="json"))
        pdf = result.metadata["pdf"]
        text = pdf["pages"][0]["text"]
        check(lang + " final success and OCR", result.success and pdf["method"] == "ocr")
        check(lang + " effective parameters", pdf["ocr"]["config"] == source.ocr.model_dump() and pdf["ocr"]["config"]["dpi"] == 96)
        check(lang + " sequential sample page number", [p["page_number"] for p in pdf["pages"]] == [1])
        check(lang + " text anchors", all(a.casefold() in "".join(text.split()).casefold() for a in anchors))
        check(lang + " final HTML Markdown", bool(result.cleaned_html.strip()) and bool(result.markdown.raw_markdown.strip()))
        INPUTS.append({"language": lang, "source_id": source.id, "url": item["url"], "original_sha256": item["sha256"],
                       "original_page": page, "sample_sha256": digest(sample.read_bytes()), "anchors": anchors,
                       "model_sha256": MODELS[lang], "seconds": round(time.monotonic() - started, 3)})
        dump(OUT / "inputs.json", INPUTS)

if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ["SEAL_WORK_DIR"] = str(WORK)
    failure = None
    try:
        asyncio.run(run())
    except Exception as exc:
        failure = str(exc)
    report = {"status": "FAIL" if failure else "PASS", "failure": failure, "checks": CHECKS, "inputs": INPUTS,
              "command": "uv run --no-sync python tests/speed_ocr_acceptance.py", "boundary_file": "tests/speed_ocr_failure_boundaries.json",
              "environment": {"python": subprocess.check_output(["python", "--version"], text=True).strip(),
                              "dependencies": {p: version(p) for p in ["crawl4ai", "pypdf", "pypdfium2"]}},
              "prerequisites": "Project uv dependencies, local tesseract and pinned fast chi_sim/chi_tra/eng/vie models; persistent experiment work originals or public PDF network access. All inputs must match URL/SHA manifest; current vie URL re-download returned a different SHA, recorded in first-download-failure.json. No DB/S3 needed.",
              "unverified": ["vie network-only recovery: current URL SHA differs; see recovery-limitations.json", "full-site harvest, DB/S3 and Results API persistence", "full-document OCR accuracy and throughput", "frozen historical Run database rows (no database access)"]}
    dump(OUT / "report.json", report)
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(bool(failure))
