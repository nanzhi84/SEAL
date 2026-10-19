"""Review checks using synthetic artifacts, with no DB, S3, or network access.

Run: uv run --no-sync python tests/review_supporting_20261019.py
Requires the project Python environment, including numpy used by the scorer.
All review evidence is disposable under work/ and artifacts/.
"""
import ast
from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work/review-20261019-supporting"
OUT = ROOT / "artifacts/review-20261019-supporting"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class HtmlEvidence(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.ids = [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])


def static_checks():
    patterns = ("dd492_network_retry.py", "dd492_real_ocr_*.py", "issue25_*.py",
                "mineru*.py", "paddle_ocr*.py", "source_ocr*.py", "speed_ocr*.py",
                "tesseract*.py")
    paths = sorted({p for pattern in patterns for p in (ROOT / "tests").glob(pattern)})
    scripts = []
    for path in paths:
        text = path.read_text()
        ast.parse(text, filename=str(path))
        scripts.append({"path": str(path.relative_to(ROOT)),
                        "lines": len(text.splitlines()),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    assert all(row["lines"] <= 500 for row in scripts)
    map_text = (ROOT / "docs/reference/尽职调查核验入口地图.md").read_text()
    rows = [line.split("|") for line in map_text.splitlines()
            if re.match(r"\| \d+ \|", line)]
    ids = [re.search(r'<a id="(dd-\d+)"', row[2]).group(1) for row in rows]
    assert ids == [f"dd-{i:03}" for i in range(1, 499)]
    classes = Counter(row[5].strip() for row in rows)
    claims = dict(re.findall(r"\*\*([A-GX])\*\*：[^\n]*?（(\d+) 条）", map_text))
    assert classes == Counter({key: int(value) for key, value in claims.items()})
    md = (ROOT / "docs/reference/公开数据源和开源采集项目调研.md").read_text()
    html = HtmlEvidence()
    html.feed((ROOT / "docs/reference/公开数据源和开源采集项目调研.html").read_text())
    md_links = re.findall(r"\]\((https?://[^)]+)\)", md)
    md_links += re.findall(r"<(https?://[^>]+)>", md)
    html_links = [link for link in html.links if link.startswith(("https://", "http://"))]
    assert Counter(md_links) == Counter(html_links)
    broken_anchors = [link for link in html.links if link.startswith("#") and link[1:] not in html.ids]
    assert not broken_anchors
    return {"scripts": scripts, "map_ids": len(ids), "map_classes": dict(classes),
            "research_external_links": len(md_links), "html_anchors": len(html.ids),
            "broken_html_anchors": broken_anchors}


def process(script, root, *args):
    copied = root / "tests" / script
    copied.parent.mkdir(parents=True, exist_ok=True)
    copied.write_bytes((ROOT / "tests" / script).read_bytes())
    result = subprocess.run([sys.executable, str(copied), *map(str, args)], cwd=root,
                            capture_output=True, text=True, timeout=30)
    return {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def mineru_failure_exit():
    root = WORK / "mineru"
    data = root / "artifacts/mineru-vlm-vietnam-20261016/result"
    data.mkdir(parents=True, exist_ok=True)
    truth = "Nghị quyết số 136/2024/QH15 26 tháng 6 năm 2024 01 tháng 01 năm 2025 "
    truth += "x" * (242 - len(truth)) + ":"
    truth_path = root / "artifacts/mineru-vietnam-20261016/ground-truth-first-paragraph.txt"
    truth_path.parent.mkdir(parents=True, exist_ok=True)
    truth_path.write_text(truth)
    # Deliberately omit page 35. The verifier must flag incomplete coverage.
    write(data / "layout.json", {"pdf_info": [{"page_idx": i} for i in range(35)]})
    write(data / "sample_content_list.json", [{"page_idx": i, "type": "text", "text": ""}
                                             for i in range(35)])
    (data / "full.md").write_text(truth)
    result = process("mineru_vlm_demo_compare.py", root)
    report = json.loads((data.parent / "acceptance.json").read_text())
    result["failed_assertions"] = [name for name, value in report["assertions"].items() if not value]
    result["coverage_label"] = report["page_coverage"]["result"]
    result["coverage_markdown"] = (data.parent / "acceptance.md").read_text()
    result["passed"] = (result["exit"] != 0 and bool(result["failed_assertions"])
                        and result["coverage_label"].startswith("FAIL")
                        and "页覆盖 PASS" not in result["coverage_markdown"])
    write(data / "layout.json", {"pdf_info": [{"page_idx": i} for i in range(36)]})
    write(data / "sample_content_list.json", [{"page_idx": i, "type": "text", "text": ""}
                                             for i in range(36)])
    positive = process("mineru_vlm_demo_compare.py", root)
    positive_report = json.loads((data.parent / "acceptance.json").read_text())
    positive["passed"] = (positive["exit"] == 0 and all(positive_report["assertions"].values())
                          and positive_report["page_coverage"]["result"].startswith("PASS"))
    return {"missing_page_rejected": result, "complete_pages_accepted": positive,
            "passed": result["passed"] and positive["passed"]}


def paddle_failure_exit():
    root = WORK / "paddle"
    base = root / "experiments/paddle-ocr-pareto"
    run = base / "artifacts/20261019"
    run.mkdir(parents=True, exist_ok=True)
    (root / "artifacts/paddle-ocr-independent-20261019").mkdir(parents=True, exist_ok=True)
    sample = base / "config/sample.txt"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text("synthetic current bytes")
    write(run / "manifest.json", {
        "input_files": {"config/sample.txt": "0" * 64}, "model_files": {}, "source_files": {},
        "runtime": {"opencv_threads": 1, "environment": {k: "1" for k in
                    ("OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OPENBLAS_NUM_THREADS",
                     "VECLIB_MAXIMUM_THREADS")}}, "timing_contract": "synthetic"})
    result = process("paddle_ocr_manual_evidence.py", root)
    report = json.loads((root / "artifacts/paddle-ocr-independent-20261019/manual-review.json").read_text())
    result["reported_passed"] = report["passed"]
    result["failed_checks"] = [row["check"] for row in report["checks"] if not row["passed"]]
    result["passed"] = result["exit"] != 0 and not report["passed"]
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["input_files"]["config/sample.txt"] = hashlib.sha256(sample.read_bytes()).hexdigest()
    model = base / "models/sample.bin"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text("synthetic model bytes")
    source = base / "runner.py"
    source.write_text("# synthetic original source\n")
    manifest["model_files"] = {"models/sample.bin": hashlib.sha256(model.read_bytes()).hexdigest()}
    manifest["source_files"] = {"runner.py": hashlib.sha256(source.read_bytes()).hexdigest()}
    write(run / "manifest.json", manifest)
    (base / "work/20261019/calibration-before-opencv-thread-limit").mkdir(parents=True, exist_ok=True)
    for index in range(3):
        write(run / f"PP-OCRv6_tiny/startup-{index}.json", {"synthetic": True})
    raw_dir = run / "PP-OCRv6_tiny/eng/page-1/0"
    write(raw_dir / "raw.json", {"res": {"rec_texts": ["test"], "text_word": [["test"]],
                                         "text_word_boxes": [[[0, 0, 1, 1]]]}})
    write(raw_dir / "words.json", [{"text": "test", "bbox": [0, 0, 1, 1], "separator": " "}])
    (raw_dir / "text.txt").write_text("test")
    positive = process("paddle_ocr_manual_evidence.py", root)
    positive_report = json.loads((root / "artifacts/paddle-ocr-independent-20261019/manual-review.json").read_text())
    positive["passed"] = positive["exit"] == 0 and positive_report["passed"]
    snapshot = root / "work/frozen-source"
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "runner.py").write_bytes(source.read_bytes())
    source.write_text("# changed current source\n")
    strict = process("paddle_ocr_manual_evidence.py", root)
    strict["passed"] = strict["exit"] != 0
    historical = process("paddle_ocr_manual_evidence.py", root, "--source-snapshot", snapshot)
    historical_report = json.loads((root / "artifacts/paddle-ocr-independent-20261019/manual-review.json").read_text())
    historical["source_snapshot"] = historical_report["source_snapshot"]
    historical["source_scope"] = historical_report["source_scope"]
    historical["passed"] = (historical["exit"] == 0 and historical_report["passed"]
                            and historical_report["source_snapshot"] == str(snapshot)
                            and "historical" in historical_report["source_scope"].lower())
    sample.write_text("tampered input")
    input_drift = process("paddle_ocr_manual_evidence.py", root, "--source-snapshot", snapshot)
    input_drift["passed"] = input_drift["exit"] != 0
    sample.write_text("synthetic current bytes")
    model.write_text("tampered model")
    model_drift = process("paddle_ocr_manual_evidence.py", root, "--source-snapshot", snapshot)
    model_drift["passed"] = model_drift["exit"] != 0
    model.write_text("synthetic model bytes")
    missing_snapshot = process("paddle_ocr_manual_evidence.py", root, "--source-snapshot", root / "missing")
    missing_snapshot["passed"] = missing_snapshot["exit"] != 0
    (snapshot / "runner.py").write_text("# incorrect frozen source\n")
    wrong_snapshot = process("paddle_ocr_manual_evidence.py", root, "--source-snapshot", snapshot)
    wrong_snapshot["passed"] = wrong_snapshot["exit"] != 0
    return {"hash_failure_rejected": result, "valid_checks_accepted": positive,
            "current_source_drift_rejected": strict, "explicit_snapshot_accepted": historical,
            "input_drift_still_rejected": input_drift, "model_drift_still_rejected": model_drift,
            "missing_snapshot_rejected": missing_snapshot, "wrong_snapshot_rejected": wrong_snapshot,
            "passed": all(row["passed"] for row in (result, positive, strict, historical, input_drift,
                                                     model_drift, missing_snapshot, wrong_snapshot))}


def frontier_incomplete_eligibility():
    # Load definitions only and redirect OUT before invoking the real scorer.
    sys.path.insert(0, str(ROOT / "tests"))
    ns = runpy.run_path(str(ROOT / "tests/tesseract_frontier_anchored_score.py"))
    out = WORK / "frontier"
    ns["main"].__globals__["OUT"] = out
    out.mkdir(parents=True, exist_ok=True)
    anchors = []
    configs = [{"configuration": "fast-144", "status": "complete", "successful_pages": 36,
                "total_seconds": 100.0},
               {"configuration": "incomplete-36", "status": "incomplete", "successful_pages": 36,
                "total_seconds": 1.0},
               {"configuration": "complete-35", "status": "complete", "successful_pages": 35,
                "total_seconds": 1.0},
               {"configuration": "incomplete-35", "status": "incomplete", "successful_pages": 35,
                "total_seconds": 1.0}]
    for page in range(1, 37):
        for config in configs:
            target = out / config["configuration"] / f"page-{page:02}.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("abc def ghi")
        for index, (reference, span) in enumerate((("abc", [0, 3]), ("def", [4, 7]),
                                                  ("ghi", [8, 11])), 1):
            anchors.append({"reference_id": f"{page:02}-{index}", "page": page,
                            "reference": reference, "status": "unique_exact", "baseline_span": span})
    write(out / "baseline-anchor-audit.json", {"anchors": anchors})
    write(out / "raw-summary.unanchored.json", {"configurations": configs})
    ns["main"]()
    results = json.loads((out / "summary.json").read_text())["configurations"]
    checks = {"complete_configuration_accepted": results[0]["fixed_quotation_sample_cer"] == 0
              and results[0]["pareto_nondominated"],
              "all_invalid_configurations_excluded": all(row["fixed_quotation_sample_cer"] is None
              and not row["pareto_nondominated"] for row in results[1:]),
              "partial_diagnostic_scores_retained": all(row["partial_resolved_sample_cer"] == 0
                                                        for row in results[1:])}
    return {"configurations": results, "checks": checks, "passed": all(checks.values())}


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    previous = OUT / "report.json"
    initial = OUT / "report-before-fixes.json"
    if previous.exists() and not initial.exists():
        initial.write_bytes(previous.read_bytes())
    report = {"command": "uv run --no-sync python tests/review_supporting_20261019.py",
              "environment": {"python": sys.version.split()[0], "network": False,
                              "database": False, "s3": False},
              "static": static_checks(), "mineru_false_success": mineru_failure_exit(),
              "paddle_false_success": paddle_failure_exit(),
              "frontier_failed_configuration": frontier_incomplete_eligibility()}
    report["passed"] = all(report[key]["passed"] for key in
                           ("mineru_false_success", "paddle_false_success", "frontier_failed_configuration"))
    write(OUT / "report.json", report)
    print(json.dumps({"static_scripts": len(report["static"]["scripts"]),
                      "passed": report["passed"],
                      "report": str(OUT / "report.json")}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
