"""Serial real-PDF acquisition benchmark, then offline scoring. Run from any cwd."""
import argparse
import importlib.metadata
import json
import os
import platform
import random
import shutil
import signal
import statistics
import subprocess
import sys
import time
from contextlib import closing

from common import CONFIG, OUT, ROOT, SETTINGS, WORK, inventory, read, relative, save, sha
from scoring import frontier, region_scores, tesseract_words


def alarm(signum, frame):
    raise TimeoutError("Page/model execution deadline exceeded")


def render(page, dpi, path):
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(ROOT / page["pdf_path"])) as document:
        with closing(document[page["page_number"] - 1]) as pdfpage:
            with closing(pdfpage.render(scale=dpi / 72)) as bitmap:
                with bitmap.to_pil() as image:
                    width, height = image.size
                    image.save(path)
    return width, height


def paddle_words(result):
    # Word/character coordinates are returned by the official inference pipeline.
    words = []
    texts, boxes = result["text_word"], result["text_word_boxes"]
    for line_texts, line_boxes in zip(texts, boxes, strict=True):
        for index, (text, box) in enumerate(zip(line_texts, line_boxes, strict=True)):
            if len(box) != 4:
                raise ValueError("Invalid Paddle word rectangle")
            words.append({"text": text, "bbox": box, "separator": " " if index == 0 else ""})
    return words


def run_page(engine, predictor, language, page, dpi, repetition, folder):
    folder.mkdir(parents=True, exist_ok=True)
    png = WORK / "page.png"
    started = time.perf_counter()
    signal.alarm(SETTINGS["page_timeout_seconds"])
    try:
        width, height = render(page, dpi, png)
        rendered = time.perf_counter()
        if engine == "tessdata_fast":
            command = ["tesseract", str(png), str(folder / "text"), "--tessdata-dir",
                       str(WORK / "models/tessdata_fast"), "-l", language, "--oem", "1",
                       "--psm", "3", "--dpi", str(dpi),
                       "-c", "tessedit_create_txt=1", "-c", "tessedit_create_tsv=1"]
            proc = subprocess.run(command, capture_output=True, text=True,
                                  timeout=SETTINGS["page_timeout_seconds"])
            (folder / "stderr.txt").write_text(proc.stderr)
            if proc.returncode:
                raise RuntimeError(f"Tesseract exited {proc.returncode}")
            words = tesseract_words((folder / "text.tsv").read_text())
        else:
            results = list(predictor.predict(str(png)))
            if len(results) != 1:
                raise RuntimeError("Expected one page result")
            raw = results[0].json
            save(folder / "raw.json", raw)
            result = raw.get("res", raw)
            words = paddle_words(result)
            (folder / "text.txt").write_text("\n".join(result["rec_texts"]) + "\n")
        save(folder / "words.json", words)
        finished = time.perf_counter()
        if not words or not (folder / "text.txt").read_text().strip():
            raise RuntimeError("Empty page OCR output")
        value = {"status": "ok", "width": width, "height": height,
                 "render_seconds": rendered - started, "ocr_seconds": finished - rendered,
                 "total_seconds": finished - started, "repetition": repetition}
    except Exception as exc:
        value = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc),
                 "total_seconds": time.perf_counter() - started, "repetition": repetition}
    finally:
        signal.alarm(0)
        png.unlink(missing_ok=True)
    value["files"] = inventory(folder)
    save(folder / "receipt.json", value)
    return value


def model_manifest():
    required = [WORK / "models/tessdata_fast" / (language + ".traineddata")
                for language in SETTINGS["languages"] if "tessdata_fast" in SETTINGS["models"]]
    cache = WORK / "paddlex-cache" / "official_models"
    required += [cache / (engine + "_" + kind) / filename
                 for engine in SETTINGS["models"] if engine != "tessdata_fast"
                 for kind in ("det", "rec")
                 for filename in ("inference.json", "inference.yml", "inference.pdiparams")]
    for path in required:
        if not path.is_file():
            raise RuntimeError("Missing prepared model: " + relative(path))
    files = inventory(WORK / "models")
    files.update({k: v for k, v in inventory(cache).items()
                  if k.rsplit("/", 1)[-1] in {"inference.json", "inference.yml", "inference.pdiparams"}})
    return files


def manifest():
    import cv2
    import pypdfium2
    if SETTINGS["cpu_threads"] != 1:
        raise RuntimeError("This benchmark requires cpu_threads=1")
    sources = {relative(p): sha(p) for p in sorted(ROOT.glob("*.py"))}
    sources.update(inventory(CONFIG))
    sources["uv.lock"] = sha(ROOT / "uv.lock")
    sources["pyproject.toml"] = sha(ROOT / "pyproject.toml")
    return {
        "path_base": "experiment_root", "config": SETTINGS,
        "command": "uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/benchmark.py",
        "prerequisites": "macOS arm64 CPU; Python 3.12 locked environment; tesseract on PATH; prepare.py completed with pinned public PDFs and all models; no DB/S3/credentials",
        "input_files": {p: v["sha256"] for p, v in read(CONFIG / "documents.json").items()},
        "model_files": model_manifest(), "source_files": sources,
        "runtime": {"platform": platform.platform(), "machine": platform.machine(),
                    "processor": subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip(),
                    "python": sys.version, "pdfium": str(pypdfium2.PDFIUM_INFO),
                    "dependencies": {p: importlib.metadata.version(p) for p in
                                     ["paddleocr", "paddlex", "paddlepaddle", "numpy", "pypdfium2"]},
                    "tesseract": subprocess.check_output(["tesseract", "--version"], text=True).splitlines()[0],
                    "tesseract_binary_sha256": sha(shutil.which("tesseract")),
                    "opencv_threads": cv2.getNumThreads(),
                    "environment": {k: os.environ.get(k) for k in
                                    ["OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"]}},
        "timing_contract": "Single CPU thread. Sum over pages of median of three render+PNG+OCR+output-write durations. Paddle persistent model load and warmup excluded and reported separately; per-page Tesseract CLI startup included. Offline score and model download excluded.",
        "scoring_contract": "NFC; Chinese strip whitespace, English/Vietnamese collapse whitespace. Fixed geometry word-box centres, original output order, no confidence filtering, global edit distance weighted by reference characters.",
        "limitations": ["16 pages from 6 documents; only one Traditional Chinese and one Vietnamese document",
                        "Fixed region CER is not full-page or language-population accuracy",
                        "Models have different segmentation; coordinate extraction is part of observed pipeline behavior",
                        "Three repeats do not establish stable differences near timing noise",
                        "Paddle persistent versus per-page Tesseract CLI deployment shapes differ"],
    }


def validate_frozen(current):
    path = OUT / "manifest.json"
    if path.exists():
        previous = read(path)
        if previous != current:
            raise RuntimeError("Input/model/runtime/source/config drift: use a new config run name")
    else:
        for p, expected in current["input_files"].items():
            if sha(ROOT / p) != expected:
                raise RuntimeError("PDF hash drift: " + p)
        save(path, current)


def score_all():
    frozen = read(OUT / "manifest.json")
    for group in ["input_files", "model_files", "source_files"]:
        for path, expected in frozen[group].items():
            if sha(ROOT / path) != expected:
                raise RuntimeError("Frozen file drift: " + path)
    references = read(CONFIG / "references.json")
    records, configs = [], []
    for engine in SETTINGS["models"]:
        for language in SETTINGS["languages"]:
            for dpi in SETTINGS["dpis"]:
                identifier = f"{engine}-{language}-{dpi}"
                selected = []
                for page in references[language]["pages"]:
                    page_id = f"{page['document_id']}-p{page['page_number']}"
                    folder = OUT / engine / language / str(dpi) / page_id
                    receipts = []
                    regions = []
                    for rep in range(SETTINGS["repeats"]):
                        receipt_path = folder / str(rep) / "receipt.json"
                        if not receipt_path.exists():
                            continue
                        receipt = read(receipt_path)
                        for path, expected in receipt["files"].items():
                            if sha(ROOT / path) != expected:
                                raise RuntimeError("Output drift: " + path)
                        receipts.append(receipt)
                        if receipt["status"] == "ok":
                            scores = region_scores(read(folder / str(rep) / "words.json"), page,
                                                   receipt["width"], receipt["height"], language)
                            if not regions:
                                regions = scores
                            elif scores != regions:
                                raise RuntimeError("Nondeterministic OCR output across repetitions")
                    eligible = len(receipts) == SETTINGS["repeats"] and all(r["status"] == "ok" for r in receipts)
                    record = {"configuration_id": identifier, "page_id": page_id, "language": language,
                              "input_path": page["pdf_path"], "input_sha256": sha(ROOT / page["pdf_path"]),
                              "page_number": page["page_number"], "eligible": eligible, "receipts": receipts,
                              "durations": [r["total_seconds"] for r in receipts], "regions": regions,
                              "median_seconds": statistics.median(r["total_seconds"] for r in receipts) if eligible else None}
                    records.append(record)
                    selected.append(record)
                eligible = all(r["eligible"] for r in selected)
                regions = [r for p in selected for r in p["regions"]]
                errors = sum(r["errors"] for r in regions)
                count = sum(r["reference_characters"] for r in regions)
                configs.append({"id": identifier, "engine": engine, "model": engine, "dpi": dpi,
                                "language": language, "eligible": eligible, "errors": errors,
                                "reference_characters": count, "cer": errors / count if eligible else None,
                                "total_seconds": sum(r["median_seconds"] for r in selected) if eligible else None})
    save(OUT / "records.json", records)
    save(OUT / "summary.json", {"configurations": frontier(configs), "manifest_sha256": sha(OUT / "manifest.json")})
    print("Scored", sum(c["eligible"] for c in configs), "/", len(configs), "configurations", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=SETTINGS["models"])
    parser.add_argument("--score-only", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGALRM, alarm)
    for key in ["OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"]:
        os.environ[key] = "1"
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(WORK / "paddlex-cache")
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    import cv2
    # macOS GCD ignores positive thread counts; zero disables OpenCV parallelism.
    cv2.setNumThreads(0)
    if cv2.getNumThreads() != 1:
        raise RuntimeError("OpenCV parallelism was not disabled")
    if args.score_only:
        score_all()
        return 0
    validate_frozen(manifest())
    references = read(CONFIG / "references.json")
    engines = [args.engine] if args.engine else SETTINGS["models"]
    for engine in engines:
        predictor = None
        started = time.perf_counter()
        if engine != "tessdata_fast":
            from prepare import paddle_model
            signal.alarm(600)
            predictor = paddle_model(engine.rsplit("_", 1)[1])
            loaded = time.perf_counter()
            page = references[SETTINGS["languages"][0]]["pages"][0]
            render(page, 120, WORK / "warmup.png")
            list(predictor.predict(str(WORK / "warmup.png")))
            (WORK / "warmup.png").unlink()
            signal.alarm(0)
            save(OUT / engine / "startup.json", {"load_seconds": loaded - started,
                 "warmup_seconds": time.perf_counter() - loaded, "included_in_frontier": False})
        jobs = [(lang, dpi, page) for lang in SETTINGS["languages"] for dpi in SETTINGS["dpis"]
                for page in references[lang]["pages"]]
        random.Random(SETTINGS["seed"]).shuffle(jobs)
        for index, (language, dpi, page) in enumerate(jobs):
            page_id = f"{page['document_id']}-p{page['page_number']}"
            for rep in range(SETTINGS["repeats"]):
                folder = OUT / engine / language / str(dpi) / page_id / str(rep)
                if (folder / "receipt.json").exists():
                    if read(folder / "receipt.json")["status"] != "ok":
                        raise RuntimeError("Retained failed attempt: inspect and select a new run")
                    continue
                value = run_page(engine, predictor, language, page, dpi, rep, folder)
                print(engine, f"{index + 1}/{len(jobs)}", language, dpi, page_id, rep,
                      value["status"], round(value["total_seconds"], 3), flush=True)
                if value["status"] != "ok":
                    print(value.get("error"), flush=True)
                    return 1
        del predictor
    score_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
