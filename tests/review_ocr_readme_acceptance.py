"""Fresh isolated CLI acceptance of the documented PDF result-consumption flow.

Failure boundaries are declared in review_ocr_config_boundaries.json. The existing
speed_ocr_acceptance.py supplies real four-language OCR coverage. This companion
executes README Python blocks against fresh local PostgreSQL/S3 namespaces and
controlled PDFs, including failed-run and receipt-mismatch cases.
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from pypdf import PdfWriter

from issue25_fixtures import FixtureServer, native_page
from issue25_lab import Lab, ROOT, implementation_digest

NAME = "review-20261019-config/readme-contract"
IDS = ["dd-017", "dd-040", "dd-041", "dd-448", "dd-492"]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def snippet(lab, label, code, cwd):
    (lab.out / (label + ".py")).write_text(code)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=cwd, env=lab.env,
        capture_output=True, text=True, timeout=180,
    )
    (lab.out / (label + ".stdout.txt")).write_text(result.stdout)
    (lab.out / (label + ".stderr.txt")).write_text(result.stderr)
    return result


def write_receipt(path, value):
    dump(path, value)


def main():
    lab = Lab(NAME)
    lab.checks.clear()
    doc = (ROOT / "README.md").read_text()
    blocks = re.findall(r"uv run --no-sync python - <<'PY'[^\n]*\n(.*?)\nPY", doc, re.S)
    if len(blocks) != 4:
        raise AssertionError("README Python blocks changed; inspect before adapting acceptance")
    config_code, storage_code, auth_code, api_code = blocks
    sandbox = lab.work / "readme-root"
    work, out = sandbox / "work/readme-demo", sandbox / "artifacts/readme-demo"
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    (sandbox / "sources").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "sources/dd-492.yaml", sandbox / "sources/dd-492.yaml")
    (lab.out / "readme.sha256").write_text(hashlib.sha256(doc.encode()).hexdigest() + "\n")
    completed = False
    try:
        result = snippet(lab, "01-derived-source", config_code, sandbox)
        lab.check("README derived source config executes", result.returncode == 0)
        derived = yaml.safe_load((work / "dd-492.yaml").read_text())
        from seal.config import load_file
        effective = load_file(work / "dd-492.yaml")
        lab.check("README retains source 96 DPI", effective.ocr.dpi == 96,
                  {"declared": derived["ocr"], "effective": effective.ocr.model_dump()})
        result = snippet(lab, "02-api-settings", auth_code, sandbox)
        from seal.api_security import Settings
        settings = Settings.load(work / "api-config.local.json")
        lab.check("README API configuration validates", result.returncode == 0 and
                  settings.attachment_max_bytes == 134217728 and
                  settings.request_timeout_seconds == 60)

        # Prove native processing does not demand an irrelevant model: README
        # intentionally installs only vie while Chinese Sources can have PDFs.
        vie_only = lab.work / "vie-only-tessdata"
        vie_only.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "work/ocr-tessdata/vie.traineddata", vie_only / "vie.traineddata")
        lab.env["TESSDATA_PREFIX"] = str(vie_only)
        proc, receipt = lab.cli("init")
        lab.check("fresh isolated storage initializes", proc.returncode == 0)
        writer = PdfWriter()
        native_page(writer, "NATIVE READ ME ACCEPTANCE ALPHA OMEGA")
        native = io.BytesIO()
        writer.write(native)
        data = {"native": native.getvalue(), "scan":
                (ROOT / "work/review-20261019-config/vie-page.pdf").read_bytes()}
        with FixtureServer(data) as server:
            paths, receipts = {}, {}
            for source_id in IDS:
                urls = [server.base + "/" + source_id + "/native.pdf"]
                lang = "chi_sim"
                if source_id == "dd-492":
                    urls.append(server.base + "/" + source_id + "/scan.pdf")
                    lang = "vie"
                paths[source_id] = lab.source(source_id, urls, extra={
                    "ocr": {"languages": lang, "dpi": 96},
                    "budget": {"requests": 100, "seconds": 180, "response_bytes": 134217728},
                })
                proc, receipts[source_id] = lab.cli("collect", paths[source_id], "--no-progress")
                lab.check(source_id + " fresh collection completes", proc.returncode == 0 and
                          receipts[source_id]["status"] == "complete")
                write_receipt(out / (source_id + "-run.json"), receipts[source_id])
            samples = {"native": server.base + "/dd-492/native.pdf",
                       "ocr": server.base + "/dd-492/scan.pdf"}
            dump(work / "dd-492-samples.json", samples)
            result = snippet(lab, "03-storage-first", storage_code, sandbox)
            lab.check("README first-run DB/S3 validation passes", result.returncode == 0)
            if result.returncode:
                return 1
            (out / "storage-check.json").write_text(result.stdout)

            # Updating a Source must leave the already frozen queued Run intact.
            proc, frozen = lab.cli("collect", paths["dd-492"], "--enqueue")
            lab.check("pending Run is queued without acquisition", proc.returncode == 0)
            with lab.connect() as conn:
                before = conn.execute("SELECT config,config_hash,parser_manifest FROM seal.run WHERE id=%s",
                                      (frozen["run_id"],)).fetchone()
            selected = yaml.safe_load(paths["dd-492"].read_text())
            selected["ocr"]["dpi"] = 144
            paths["dd-492"].write_text(yaml.safe_dump(selected))
            _, newer = lab.cli("collect", paths["dd-492"], "--enqueue")
            with lab.connect() as conn:
                after = conn.execute("SELECT config,config_hash,parser_manifest FROM seal.run WHERE id=%s",
                                     (frozen["run_id"],)).fetchone()
                new = conn.execute("SELECT config FROM seal.run WHERE id=%s", (newer["run_id"],)).fetchone()
            lab.check("old frozen configuration/hash/manifest remain byte-equivalent", before == after)
            lab.check("new Run selects new DPI while old Run keeps 96",
                      before["config"]["ocr"]["dpi"] == 96 and new["config"]["ocr"]["dpi"] == 144)
            selected["ocr"]["dpi"] = 96
            paths["dd-492"].write_text(yaml.safe_dump(selected))

            # First receipt still identifies its own successful Run after later
            # queued Runs. Latest-run queries would incorrectly reject it.
            result = snippet(lab, "04-storage-exact-run", storage_code, sandbox)
            lab.check("README selects recorded Run rather than latest unrelated Run", result.returncode == 0)

            # A fresh unchanged Observation may reuse a successful old Version.
            proc, unchanged = lab.cli("collect", paths["dd-492"], "--no-progress")
            write_receipt(out / "dd-492-run.json", unchanged)
            with lab.connect() as conn:
                observations = conn.execute("SELECT outcome,version_id FROM seal.observation WHERE run_id=%s",
                                            (unchanged["run_id"],)).fetchall()
            lab.check("fixture produced current unchanged observations", proc.returncode == 0 and
                      len(observations) == 2 and all(o["outcome"] == "unchanged" and
                      o["version_id"] for o in observations))
            result = snippet(lab, "05-storage-unchanged", storage_code, sandbox)
            lab.check("README accepts successful current unchanged observations", result.returncode == 0)
            (out / "storage-check.json").write_text(result.stdout)

            # Run identity must bind to the claimed Source.
            wrong = dict(unchanged, run_id=receipts["dd-017"]["run_id"])
            write_receipt(out / "dd-492-run.json", wrong)
            result = snippet(lab, "06-storage-wrong-source-run", storage_code, sandbox)
            lab.check("README rejects receipt Run from a different Source", result.returncode != 0)
            write_receipt(out / "dd-492-run.json", unchanged)

            # A failed acquisition cannot borrow current_version_id left from
            # earlier successes. The external website is controlled by fixture.
            data["native"] = data["scan"] = b"%PDF-1.7\nBROKEN ACCEPTANCE INPUT\n%%EOF"
            proc, failed = lab.cli("collect", paths["dd-492"], "--no-progress")
            with lab.connect() as conn:
                failures = conn.execute("SELECT outcome,version_id FROM seal.observation WHERE run_id=%s",
                                        (failed["run_id"],)).fetchall()
            lab.check("fixture produced failed current observations and retained earlier Versions",
                      proc.returncode != 0 and failed["status"] == "failed" and len(failures) == 2 and
                      all(o["version_id"] is None for o in failures))
            # Even a forged complete field in the file must not override DB state.
            write_receipt(out / "dd-492-run.json", dict(failed, status="complete"))
            result = snippet(lab, "07-storage-failed-current-run", storage_code, sandbox)
            lab.check("README rejects current failed Run despite retained old Versions", result.returncode != 0)
            write_receipt(out / "dd-492-run.json", unchanged)

            # Publish known successful Versions and execute the literal API
            # consumer against an isolated port, adapting only its base URL.
            proc, created = lab.cli("snapshot", "create")
            lab.check("snapshot creation succeeds", proc.returncode == 0)
            proc, _ = lab.cli("snapshot", "publish", created["snapshot_id"])
            lab.check("snapshot explicit publication succeeds", proc.returncode == 0)
            lab.start_api()
            (work / "headers.local.txt").write_text("Authorization: Bearer " + lab.bearer + "\n")
            (work / "headers.local.txt").chmod(0o600)
            code = api_code.replace("http://127.0.0.1:8090", lab.base)
            result = snippet(lab, "08-api-consume", code, sandbox)
            lab.check("README authenticated API pages, Markdown and attachment hashes pass",
                      result.returncode == 0)
            paged = snippet(lab, "09-api-consume-pagination", code.replace('"limit": 100', '"limit": 1'), sandbox)
            lab.check("README Link loop consumes both samples across real one-item pages",
                      paged.returncode == 0 and len(paged.stdout.splitlines()) == 2)
            for method in samples:
                path = out / ("dd-492-" + method + ".pdf")
                lab.check(method + " PDF downloaded by documented consumer", path.is_file())
            for path in out.glob("dd-492-*.*"):
                if path.name.endswith("run.json"):
                    continue
                shutil.copyfile(path, lab.out / path.name)

        # Stage a historical frozen row with no OCR key; no current YAML load is
        # allowed to normalize that persisted historical input silently.
        lab.env["TESSDATA_PREFIX"] = str(ROOT / "work/ocr-tessdata")
        legacy_native = io.BytesIO()
        writer.write(legacy_native)
        with FixtureServer({"native": legacy_native.getvalue()}) as server:
            legacy_path = lab.source("dd-033", [server.base + "/legacy/native.pdf"])
            from seal.core import uid
            from seal.db import j
            from seal.runs import create_run
            previous_env = dict(os.environ)
            try:
                os.environ.update(lab.env)
                legacy_config = load_file(legacy_path).model_dump()
                legacy_config.pop("ocr")
                with lab.connect() as conn:
                    conn.execute("INSERT INTO seal.source(id,config) VALUES(%s,%s)", ("dd-033", j(legacy_config)))
                    conn.execute("INSERT INTO seal.frontier(id,source_id) VALUES(%s,%s)", (uid(), "dd-033"))
                    legacy_id = create_run("dd-033", "collect", conn)
                    frozen = conn.execute("SELECT config,config_hash FROM seal.run WHERE id=%s", (legacy_id,)).fetchone()
                result = subprocess.run([sys.executable, "-c",
                    "from seal.runs import execute_run; import json; print(json.dumps(execute_run(" +
                    repr(legacy_id) + "),default=str))"], cwd=ROOT, env=lab.env,
                    capture_output=True, text=True, timeout=120)
                with lab.connect() as conn:
                    current = conn.execute("SELECT config,config_hash,status,parser_manifest FROM seal.run WHERE id=%s",
                                           (legacy_id,)).fetchone()
                lab.check("historical missing-OCR Run executes with 144-DPI default",
                          result.returncode == 0 and current["status"] == "complete" and
                          current["parser_manifest"]["pdf"]["ocr"]["dpi"] == 144)
                lab.check("historical Run config and hash never gain an OCR key",
                          "ocr" not in current["config"] and current["config"] == frozen["config"] and
                          current["config_hash"] == frozen["config_hash"])
                dump(lab.out / "frozen-run-evidence.json", {"frozen": before, "after": after,
                     "historical": current, "source_updated_dpi": new["config"]["ocr"]["dpi"]})
            finally:
                os.environ.clear()
                os.environ.update(previous_env)
        lab.check("README unchanged during run", doc == (ROOT / "README.md").read_text())
        lab.check("implementation unchanged during acceptance",
                  implementation_digest() == lab.environment["implementation_sha256"])
        failed = sum(c["status"] == "FAIL" for c in lab.checks)
        print(json.dumps({"passed": len(lab.checks) - failed, "failed": failed,
                          "report": str(lab.out / "report.json")}, ensure_ascii=False))
        completed = True
        return bool(failed)
    finally:
        lab.close()
        report_path = lab.out / "report.json"
        report = json.loads(report_path.read_text())
        report.update({
            "status": "PASS" if completed and all(c["status"] == "PASS" for c in lab.checks) else "FAIL",
            "command": "uv run --no-sync python tests/review_ocr_readme_acceptance.py",
            "boundary_file": "tests/review_ocr_config_boundaries.json",
            "prerequisites": "Project uv Python 3.12 environment; Docker engine; cached postgres:17-alpine and seal-minio:RELEASE.2025-10-15T17-29-55Z; work/ocr-tessdata models; first run speed_ocr_acceptance.py with SPEED_OCR_RUN=review-20261019-config to produce retained public vie-page.pdf. This script provisions and removes only its own ephemeral storage containers, with fresh database and bucket.",
            "readme_sha256": hashlib.sha256(doc.encode()).hexdigest(),
            "unverified": [
                "Current official PDF URLs and whole public Source network harvesting",
                "OCR quality of complete multi-page public documents",
                "README operating-system package/model download commands",
                "Production deployment or existing production database rows",
            ],
            "adaptations": [
                "Public image-only vie first page retained by preceding OCR acceptance; synthetic native PDF served from authorized loopback URLs",
                "README Python storage and API blocks execute unchanged except API base URL uses isolated free port",
                "Second API consumption changes limit from 100 to 1 to force actual pagination",
            ],
        })
        dump(report_path, report)


if __name__ == "__main__":
    from review_ocr_storage import IsolatedStorage

    with IsolatedStorage(ROOT / "work" / NAME) as storage:
        os.environ.update(storage.env)
        raise SystemExit(main())
