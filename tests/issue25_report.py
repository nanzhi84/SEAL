"""Rebuild the final evidence index without credentials or signed capabilities."""

import argparse
import hashlib
import json
import shlex
import subprocess
import time

from issue25_lab import ROOT, implementation_digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="issue25-final-" + time.strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--reports", nargs="+", required=True)
    args = parser.parse_args()
    out = ROOT / "artifacts" / args.name
    out.mkdir(parents=True, exist_ok=True)
    reports = []
    for name in args.reports:
        path = ROOT / "artifacts" / name / "report.json"
        report = json.loads(path.read_text())
        checks = report["checks"]
        reports.append({
            "name": name, "report": str(path.relative_to(ROOT)),
            "passed": sum(c["status"] == "PASS" for c in checks),
            "failed": sum(c["status"] == "FAIL" for c in checks),
            "implementation_sha256": report["environment"]["implementation_sha256"],
            "reproduce_command": " ".join(shlex.split(report["command"])[:5]),
            "frozen": any(c["name"] == "implementation_frozen_during_acceptance" and c["status"] == "PASS" for c in checks),
        })
    hashes = {r["implementation_sha256"] for r in reports}
    frozen = len(hashes) == 1 and implementation_digest() in hashes and all(r["frozen"] for r in reports)
    failed = sum(r["failed"] for r in reports)
    summary = {
        "status": "PASS" if not failed and frozen else "FAIL",
        "passed": sum(r["passed"] for r in reports), "failed": failed,
        "implementation_frozen_and_current": frozen,
        "reports": reports,
        "test_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted((ROOT / "tests").glob("issue25_*")) if path.is_file()},
        "reproduction": "Run commands from repository root, with the configured local PostgreSQL/MinIO services and admin credentials in .env.local; Tesseract 5.5.3 and the recorded official eng/chi_sim/vie models. Default names generate a fresh isolated database/bucket and separate work/artifact paths. Do not reuse --name for a previous fault run with a retained blocked marker.",
        "unverified": ["production/public-source full harvest", "Linux/cgroup/container deployment behavior",
                       "parent SIGKILL, machine power loss instantaneous cleanup"],
        "accepted_scope": "Processing success is complete structurally valid nonempty document output. OCR accuracy/layout/table restoration are not gates. Mixed/page-number-only scans remain native and can omit scanned body.",
    }
    issue = subprocess.check_output(["gh", "issue", "view", "25", "--repo", "nanzhi84/SEAL",
                                     "--json", "title,body,url,state"], cwd=ROOT)
    (out / "issue-contract.json").write_bytes(issue)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({"status": summary["status"], "passed": summary["passed"],
                      "failed": failed, "summary": str(out / "summary.json")}))
    return summary["status"] != "PASS"


if __name__ == "__main__":
    raise SystemExit(main())
