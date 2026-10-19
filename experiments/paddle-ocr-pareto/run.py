"""Fresh-run bootstrap, resumable screening, and report generation entrypoint."""
import argparse
import fcntl
import os

from common import OUT, ROOT, WORK, save, sha


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    for key in ["OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"]:
        os.environ[key] = "1"
    import cv2
    cv2.setNumThreads(0)
    OUT.mkdir(parents=True, exist_ok=True)
    if not args.report_only:
        from benchmark import manifest, validate_frozen
        validate_frozen(manifest())
    if not args.report_only:
        from screen import main as screen
        screen()
    else:
        from screen import summarize, verify_manifest
        verify_manifest()
        summarize(require_confirmed=True)
    from screen_report import main as report
    report()
    from figure import main as figure
    figure()
    path = OUT / "report.md"
    path.write_text(path.read_text().replace("paddle-ocr-pareto/screen.py", "paddle-ocr-pareto/run.py"))
    save(OUT / "entry-receipt.json", {
        "path_base": "experiment_root", "source_files": {
            "run.py": sha(ROOT / "run.py"), "figure.py": sha(ROOT / "figure.py")},
        "command": "uv run --project experiments/paddle-ocr-pareto python experiments/paddle-ocr-pareto/run.py",
        "preparation": "uv sync --project experiments/paddle-ocr-pareto --frozen; run prepare.py to restore pinned public PDFs and all six Paddle model directories before this entrypoint",
        "report_only": args.report_only,
    })


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another experiment entrypoint owns this run") from None
        main()
