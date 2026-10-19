"""User-selected coarse screening followed by repeated frontier confirmation."""
import gc
import os
import signal
import statistics
import time

from benchmark import alarm, render, run_page
from common import CONFIG, OUT, ROOT, SETTINGS, WORK, read, relative, save, sha
from scoring import frontier, region_scores

POLICY = read(CONFIG / "screening.json")
REFERENCES = read(CONFIG / "references.json")


def verify_manifest():
    frozen = read(OUT / "manifest.json")
    for group in ["input_files", "model_files", "source_files"]:
        for path, expected in frozen[group].items():
            if sha(ROOT / path) != expected:
                raise RuntimeError("Frozen original file drift: " + path)
    extra = {"user_scope": "Explicit user choice: screen first, repeat candidate frontier points",
             "original_manifest_sha256": sha(OUT / "manifest.json"), "policy": POLICY,
             "source_files": {relative(p): sha(p) for p in
                              [ROOT / "screen.py", ROOT / "screen_report.py", CONFIG / "screening.json"]},
             "timing": "Each page median of available repeats; screened points can have 1 repeat; final frontier requires at least 3 per page. Original Tesseract/tiny 3-repeat data retained unchanged.",
             "limitations": POLICY["limitations"]}
    path = OUT / "screening-manifest.json"
    if path.exists() and read(path) != extra:
        raise RuntimeError("Screening source/policy drift")
    save(path, extra)


def summarize(*, require_confirmed=False):
    records, configurations = [], []
    for engine in SETTINGS["models"]:
        for language in SETTINGS["languages"]:
            for dpi in SETTINGS["dpis"]:
                identifier = f"{engine}-{language}-{dpi}"
                pages = []
                for page in REFERENCES[language]["pages"]:
                    page_id = f"{page['document_id']}-p{page['page_number']}"
                    folder = OUT / engine / language / str(dpi) / page_id
                    receipts, scores = [], []
                    for rep in range(SETTINGS["repeats"]):
                        path = folder / str(rep) / "receipt.json"
                        if not path.exists():
                            continue
                        receipt = read(path)
                        for name, expected in receipt["files"].items():
                            if sha(ROOT / name) != expected:
                                raise RuntimeError("Output hash drift: " + name)
                        receipts.append(receipt)
                        if receipt["status"] == "ok":
                            current = region_scores(read(folder / str(rep) / "words.json"), page,
                                                    receipt["width"], receipt["height"], language)
                            if scores and current != scores:
                                raise RuntimeError("OCR repeat output drift: " + identifier)
                            scores = current
                    valid = bool(receipts) and all(r["status"] == "ok" for r in receipts)
                    record = {"configuration_id": identifier, "page_id": page_id, "language": language,
                              "input_path": page["pdf_path"], "input_sha256": sha(ROOT / page["pdf_path"]),
                              "page_number": page["page_number"], "eligible": valid,
                              "receipts": receipts, "durations": [r["total_seconds"] for r in receipts],
                              "regions": scores, "minimum_repeats": len(receipts),
                              "median_seconds": statistics.median(r["total_seconds"] for r in receipts) if valid else None}
                    pages.append(record)
                    if receipts:
                        records.append(record)
                valid = all(p["eligible"] for p in pages)
                repeated = valid and all(p["minimum_repeats"] >= POLICY["confirmation_repeats"] for p in pages)
                regions = [r for p in pages for r in p["regions"]]
                errors = sum(r["errors"] for r in regions)
                denominator = sum(r["reference_characters"] for r in regions)
                attempted = any(p["receipts"] for p in pages)
                failed = any(r["status"] != "ok" for p in pages for r in p["receipts"])
                stage = ("replicated" if repeated else "screened" if valid else
                         "failed" if failed else "incomplete" if attempted else "not_evaluated")
                configurations.append({"id": identifier, "engine": engine, "model": engine,
                    "language": language, "dpi": dpi, "eligible": valid,
                    "stage": stage,
                    "errors": errors, "reference_characters": denominator,
                    "minimum_repeats": min(p["minimum_repeats"] for p in pages),
                    "cer": errors / denominator if valid else None,
                    "total_seconds": sum(p["median_seconds"] for p in pages) if valid else None})
    summary = {"configurations": frontier(configurations), "manifest_sha256": sha(OUT / "manifest.json"),
               "screening_manifest_sha256": sha(OUT / "screening-manifest.json"),
               "policy": POLICY, "scope": "Empirical union frontier over evaluated configurations; not exhaustive over untested DPI"}
    if require_confirmed and any(r["pareto"] and r["stage"] != "replicated"
                                 for r in summary["configurations"]):
        raise RuntimeError("An unconfirmed point remains on the frontier")
    save(OUT / "records.json", records)
    save(OUT / "summary.json", summary)
    return summary


class Models:
    def __init__(self):
        self.loaded = {}

    def get(self, engine):
        if engine == "tessdata_fast":
            return None
        if engine not in self.loaded:
            from prepare import paddle_model
            started = time.perf_counter()
            signal.alarm(600)
            predictor = paddle_model(engine.rsplit("_", 1)[1])
            loaded = time.perf_counter()
            render(REFERENCES["chi_sim"]["pages"][0], 120, WORK / "screen-warmup.png")
            list(predictor.predict(str(WORK / "screen-warmup.png")))
            (WORK / "screen-warmup.png").unlink()
            signal.alarm(0)
            self.loaded[engine] = predictor
            path = OUT / engine / "screening-startups.json"
            attempts = read(path) if path.exists() else []
            attempts.append({"load_seconds": loaded - started,
                             "warmup_seconds": time.perf_counter() - loaded,
                             "included_in_frontier": False})
            save(path, attempts)
        return self.loaded[engine]

    def ensure(self, engine, language, dpi, repeats, phase):
        # Keep each configuration complete across all four pages before moving on.
        for page in REFERENCES[language]["pages"]:
            page_id = f"{page['document_id']}-p{page['page_number']}"
            for rep in range(repeats):
                folder = OUT / engine / language / str(dpi) / page_id / str(rep)
                path = folder / "receipt.json"
                if path.exists():
                    if read(path)["status"] != "ok":
                        raise RuntimeError("Retained failed page attempt: " + relative(path))
                    continue
                predictor = self.get(engine)
                value = run_page(engine, predictor, language, page, dpi, rep, folder)
                print(phase, engine, language, dpi, page_id, rep, value["status"],
                      round(value["total_seconds"], 3), flush=True)
                if value["status"] != "ok":
                    raise RuntimeError(value.get("error", "OCR failed"))


def candidates(rows):
    evaluated = [r for r in rows if r["eligible"]]
    guard = POLICY["time_guard_ratio"]
    return [r for r in evaluated if not any(
        q["language"] == r["language"] and q["cer"] <= r["cer"]
        and q["total_seconds"] < r["total_seconds"] / guard for q in evaluated)]


def main():
    for key in ["OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"]:
        os.environ[key] = "1"
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(WORK / "paddlex-cache")
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    import cv2
    cv2.setNumThreads(0)
    if cv2.getNumThreads() != 1:
        raise RuntimeError("OpenCV parallelism remains enabled")
    signal.signal(signal.SIGALRM, alarm)
    verify_manifest()
    models = Models()
    for engine in SETTINGS["models"]:
        for language in SETTINGS["languages"]:
            for dpi in POLICY["coarse_dpis"]:
                models.ensure(engine, language, dpi, 1, "coarse")
        summarize()
        print("COARSE COMPLETE", engine, flush=True)
    summary = summarize()
    initial = candidates(summary["configurations"])
    save(OUT / "screening-selection.json", {"policy": POLICY, "initial_candidates": initial})
    jobs = {(r["engine"], r["language"], dpi) for r in initial
            for dpi in POLICY["refinement_neighbors"].get(str(r["dpi"]), [])}
    for engine, language, dpi in sorted(jobs):
        models.ensure(engine, language, dpi, 1, "adjacent")
    for round_number in range(1, len(SETTINGS["models"]) * len(SETTINGS["languages"]) * len(SETTINGS["dpis"]) + 1):
        summary = summarize()
        promote = [r for r in candidates(summary["configurations"]) if r["stage"] != "replicated"]
        print("CONFIRM ROUND", round_number, "configurations", len(promote), flush=True)
        if not promote:
            break
        save(OUT / f"confirmation-round-{round_number}.json", promote)
        for row in promote:
            models.ensure(row["engine"], row["language"], row["dpi"], POLICY["confirmation_repeats"], "confirm")
    else:
        raise RuntimeError("Candidate confirmation did not converge")
    summary = summarize()
    if any(r["pareto"] and r["stage"] != "replicated" for r in summary["configurations"]):
        raise RuntimeError("An unconfirmed point remains on the frontier")
    verify_manifest()
    gc.collect()
    print("SCREENING AND FRONTIER CONFIRMATION COMPLETE", flush=True)


if __name__ == "__main__":
    main()
