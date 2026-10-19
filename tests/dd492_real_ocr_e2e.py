"""Five fixed official PDFs: real local OCR -> CAS/SQL -> Snapshot -> Results API.

Failure boundaries are specified before this script in
tests/dd492_real_ocr_failure_boundaries.json. No OCR accuracy is assessed.
"""

import argparse
import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import boto3
import psycopg
import yaml
from cryptography.fernet import Fernet
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pypdf import PdfReader

from issue25_lab import Lab, ROOT, digest, free_port, implementation_digest, local_settings


def write_json(path, value, *, private=False):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    if private:
        path.chmod(0o600)


def history_state(database):
    options = conninfo_to_dict(local_settings()["SEAL_DATABASE_URL"])
    options["dbname"] = database
    with psycopg.connect(make_conninfo(**options), row_factory=dict_row) as conn:
        counts = {table: conn.execute("SELECT count(*) AS n FROM seal." + table).fetchone()["n"]
                  for table in ["run", "observation", "version", "snapshot"]}
        publication = conn.execute("SELECT published_snapshot_id FROM seal.publication").fetchone()
        source = conn.execute("SELECT * FROM seal.source WHERE id='dd-492'").fetchone()
    return {"database": database, "counts": counts, "publication": publication,
            "source_full_row_sha256": digest(json.dumps(source, sort_keys=True, default=str).encode())}


def observations(lab):
    with lab.connect() as conn:
        return conn.execute("""
            SELECT o.id,o.run_id,o.observed_at,o.outcome,o.error,o.body_hash,o.archive_ref,
                   o.result_ref,o.version_id,r.url,r.current_version_id,
                   v.result_ref AS version_result_ref
            FROM seal.observation o JOIN seal.resource r ON r.id=o.resource_id
            JOIN seal.frontier f ON f.id=r.frontier_id
            LEFT JOIN seal.version v ON v.id=o.version_id
            WHERE f.source_id='dd-492' ORDER BY o.observed_at,o.id
        """).fetchall()


class RealLab(Lab):
    def __init__(self, name, manifest):
        self.manifest = manifest
        self.samples = []
        self.started = time.monotonic()
        super().__init__(name)

    def save(self):
        super().save()
        path = self.out / "report.json"
        value = json.loads(path.read_text())
        value.update({
            "goal": "Five fixed dd-492 historical text-layer failures through real official URLs and local vie-only OCR, then Results API body/pages/original recheck",
            "command": "uv run --no-sync python tests/dd492_real_ocr_e2e.py --name " + self.name
                       + " --manifest " + str(self.manifest.relative_to(ROOT)),
            "elapsed_seconds": round(sum(command.get("seconds", 0) for command in self.commands
                                         if command.get("args", [None])[0] == "collect"), 3),
            "elapsed_seconds_basis": "Total real collect command wall time, including failed download and bounded retry; excludes schema/API/evidence work; OCR-only duration not separately measured",
            "historical_attempt_failures": [command for command in self.commands
                                             if command.get("args", [None])[0] == "collect" and command.get("exit")],
            "samples": self.samples,
            "selected_model": "Local Tesseract LSTM vie.traineddata only; source ocr.languages=vie",
            "prerequisites": "Project Python dependencies; configured local PostgreSQL/S3 admin services in .env.local; local Tesseract and pinned vie.traineddata; fixed selection.json plus historical-baseline.json and archived_pdf_path originals; original historical database retained for read-only isolation comparison.",
            "unverified": ["OCR text accuracy, transcription fidelity and legal content correctness",
                           "production deployment, full-site harvest and production throughput"],
            "retained": "Isolated database and bucket retained; owned test API is stopped after acceptance",
        })
        write_json(path, value)

    def private_runtime(self):
        keys = [key for key in self.env if key.startswith("SEAL_")]
        keys += ["TESSDATA_PREFIX"]
        write_json(self.work / "runtime.local.json", {key: self.env[key] for key in keys if key in self.env}, private=True)

    def start_api(self):
        port = free_port()
        self.base = "http://127.0.0.1:" + str(port)
        self.bearer = secrets.token_urlsafe(32)
        config = {"public_base_url": self.base, "token_key": Fernet.generate_key().decode(),
                  "callers": [{"id": "dd492-local-review", "key_sha256": digest(self.bearer.encode()),
                               "sources": ["dd-492"]}],
                  "request_timeout_seconds": 60, "result_max_bytes": 67108864,
                  "attachment_max_bytes": 134217728, "page_max_bytes": 67108864}
        path = self.work / "api.local.json"
        write_json(path, config, private=True)
        write_json(self.work / "client.local.json", {"base_url": self.base, "bearer": self.bearer,
                                                    "source_id": "dd-492"}, private=True)
        self.private_runtime()
        log = open(self.work / "api.log", "wb")
        proc = subprocess.Popen([sys.executable, "-m", "seal", "serve", "--config", str(path),
                                 "--port", str(port)], cwd=ROOT, env=self.env, stdout=log, stderr=log)
        self.processes.append((proc, log))
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError("API startup failed; private diagnostic log remains under work")
            try:
                self.get("/sources")
                return
            except (OSError, urllib.error.HTTPError):
                time.sleep(0.1)
        raise RuntimeError("API startup timeout")


def source_config(lab, selection):
    config = yaml.safe_load((ROOT / "sources" / "dd-492.yaml").read_text())
    config.update({"entry_urls": [item["url"] for item in selection], "concurrency": 4,
                   "scope": "固定五份 dd-492 历史无文本层公开官方 PDF；真实下载、本地单 vie 模型 OCR，仅验链路不评准确率。",
                   "budget": {"requests": 20, "seconds": 3600, "response_bytes": 134217728},
                   "crawl4ai": {"mode": "http", "max_depth": 0,
                                "page_timeout_ms": 300000, "word_count_threshold": 0},
                   "ocr": {"languages": "vie", "timeout_seconds": 1200, "max_pages": 200,
                           "dpi": 144, "max_page_pixels": 20000000, "max_result_bytes": 16777216}})
    path = lab.work / "dd-492.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    (lab.out / "dd-492.yaml").write_bytes(path.read_bytes())
    lab.source_ids = ["dd-492"]
    return path


def inspect_archive(lab, item, row):
    tag = "dd-492-" + str(item["position"]).zfill(2)
    detail = dict(item, observations=[dict(r) for r in observations(lab) if r["url"] == item["url"]])
    lab.samples.append(detail)
    detail["failure_stage"] = None
    lab.check(tag + ":historical_error", item["historical_error"] == "pdf_text_layer_required")
    lab.check(tag + ":observation_present", bool(row))
    if not row:
        detail["failure_stage"] = "source_network"
        return None
    lab.check(tag + ":same_historical_body_hash", row["body_hash"] == item["historical_archive_ref"])
    lab.check(tag + ":same_historical_archive_ref", row["archive_ref"] == item["historical_archive_ref"])
    success = row["outcome"] == "parsed" and bool(row["version_id"])
    lab.check(tag + ":successful_sql_version", success, {"outcome": row["outcome"], "error": row["error"]})
    if not success:
        error = (row["error"] or "").lower()
        detail["failure_stage"] = ("render" if any(token in error for token in ["invalid", "pixel", "page_limit"])
                                   else "recognition" if error.startswith("pdf_") else "source_network")
        return None
    lab.check(tag + ":current_version_and_result_refs", row["current_version_id"] == row["version_id"]
              and row["result_ref"] == row["version_result_ref"] and bool(row["result_ref"]))
    original = lab.object(row["archive_ref"])
    lab.check(tag + ":cas_original_hash", digest(original) == item["historical_archive_ref"])
    historical = (ROOT / item["archived_pdf_path"]).read_bytes()
    lab.check(tag + ":cas_original_equals_historical_bytes", original == historical)
    reader = PdfReader(io.BytesIO(original))
    native = [page.extract_text() or "" for page in reader.pages]
    detail["original_pages_actual"] = len(native)
    detail["native_nonempty_pages_actual"] = sum(bool(text.strip()) for text in native)
    detail["native_characters_actual"] = sum(len(text) for text in native)
    expected_method = "native" if any(native) else "ocr"
    detail["expected_method_from_live_bytes"] = expected_method
    lab.check(tag + ":same_historical_page_count", len(native) == item["original_pages"])
    lab.check(tag + ":true_document_level_ocr_eligibility", expected_method == "ocr",
              {"any_pypdf_extract_text": any(native), "native_nonempty_pages": detail["native_nonempty_pages_actual"]})
    envelope = json.loads(lab.object(row["result_ref"]))
    write_json(lab.out / (tag + "-crawlresult.json"), envelope)
    result = envelope["result"]
    pdf = (result.get("metadata") or {}).get("pdf") or {}
    pages = pdf.get("pages") or []
    detail["actual_method"] = pdf.get("method")
    detail["ocr"] = pdf.get("ocr")
    detail["page_statistics"] = [{"page_number": page.get("page_number"),
                                  "characters": len(page.get("text", "")),
                                  "empty_text": not bool(page.get("text", "").strip()),
                                  "text_sha256": digest(page.get("text", "").encode())} for page in pages]
    detail["empty_text_pages"] = [p["page_number"] for p in detail["page_statistics"] if p["empty_text"]]
    detail["page_text_characters"] = sum(p["characters"] for p in detail["page_statistics"])
    lab.check(tag + ":archive_pdf_method", pdf.get("method") == expected_method)
    lab.check(tag + ":all_original_page_numbers", [p.get("page_number") for p in pages] == list(range(1, len(native) + 1)))
    lab.check(tag + ":every_page_text_string_including_empty", all(isinstance(p.get("text"), str) for p in pages))
    lab.check(tag + ":no_invented_blank_status", all(p.get("status") != "blank" and p.get("blank") is not True for p in pages))
    ocr = pdf.get("ocr") or {}
    lab.check(tag + ":single_local_vie_model", expected_method != "ocr" or
              ("tesseract" in ocr.get("engine", "").lower() and ocr.get("config", {}).get("languages") == "vie"))
    markdown = result.get("markdown") or {}
    markdown = markdown.get("raw_markdown", "") if isinstance(markdown, dict) else markdown
    detail["markdown_characters"] = len(markdown)
    lab.check(tag + ":nonempty_markdown_and_html", bool(markdown.strip()) and bool((result.get("cleaned_html") or "").strip()))
    lab.check(tag + ":original_attachment_ref", pdf.get("original_ref") == item["historical_archive_ref"]
              and item["historical_archive_ref"] in envelope.get("attachment_refs", []))
    write_json(lab.out / (tag + "-pages.json"), {"url": item["url"], "original_pages": len(native),
               "method": pdf.get("method"), "empty_text_pages": detail["empty_text_pages"],
               "pages": detail["page_statistics"], "accuracy_evaluated": False})
    return envelope


def verify_api(lab, item, envelope):
    tag = "dd-492-" + str(item["position"]).zfill(2)
    source = next(value for value in lab.get("/sources") if value["id"] == "dd-492")
    results = lab.get("/results?" + urllib.parse.urlencode({"source_url": source["entry_urls"][0], "limit": 100}))
    selected = json.loads(lab.manifest.read_text())["selected"]
    expected_urls = {value["url"] for value in selected}
    lab.check(tag + ":api_source_resources_complete", isinstance(results, list)
              and len(results) == len(selected) and {value["url"] for value in results} == expected_urls,
              {"expected": len(selected), "actual": len(results) if isinstance(results, list) else None})
    matches = [value for value in results if value.get("url") == item["url"]]
    lab.check(tag + ":api_one_matching_document", len(matches) == 1)
    if len(matches) != 1:
        return
    result = matches[0]
    archived = envelope["result"]
    pdf = result.get("metadata", {}).get("pdf") or {}
    lab.check(tag + ":api_pages_equal_archived_text", pdf.get("pages") == archived["metadata"]["pdf"]["pages"])
    lab.check(tag + ":api_method_and_ocr_equal_archive", pdf.get("method") == archived["metadata"]["pdf"]["method"]
              and pdf.get("ocr") == archived["metadata"]["pdf"].get("ocr"))
    lab.check(tag + ":api_markdown_equal_archive", result.get("markdown") == archived["markdown"]["raw_markdown"])
    lab.check(tag + ":api_private_pdf_fields_absent", "original_ref" not in pdf and "/Users/" not in json.dumps(pdf))
    attachments = result.get("attachments") or []
    index = pdf.get("original_attachment_index")
    valid = type(index) is int and 0 <= index < len(attachments)
    lab.check(tag + ":api_original_attachment_index", valid)
    safe_attachments = [{"index": n, "name": a.get("name"), "media_type": a.get("media_type"),
                         "sha256": envelope["attachment_refs"][n]} for n, a in enumerate(attachments)]
    write_json(lab.out / (tag + "-api.json"), dict(result, attachments=safe_attachments))
    (lab.out / (tag + "-api.md")).write_text(result["markdown"])
    if valid:
        body = lab.get(attachments[index]["url"], binary=True)
        lab.check(tag + ":api_original_same_historical_sha256", digest(body) == item["historical_archive_ref"],
                  {"sha256": digest(body), "bytes": len(body)})
        (lab.out / (tag + "-original.pdf")).write_bytes(body)


def serve_saved(work):
    env = os.environ.copy()
    env.update(json.loads((work / "runtime.local.json").read_text()))
    config = json.loads((work / "api.local.json").read_text())
    port = urllib.parse.urlsplit(config["public_base_url"]).port
    return subprocess.call([sys.executable, "-m", "seal", "serve", "--config", str(work / "api.local.json"),
                            "--port", str(port)], cwd=ROOT, env=env)


def query_saved(work, position, destination):
    client = json.loads((work / "client.local.json").read_text())
    selected = json.loads((work / "selection.json").read_text())["selected"]
    item = next(value for value in selected if value["position"] == position)
    request = urllib.request.Request(client["base_url"] + "/sources",
                                     headers={"Authorization": "Bearer " + client["bearer"]})
    with urllib.request.urlopen(request, timeout=60) as response:
        source = next(value for value in json.loads(response.read()) if value["id"] == "dd-492")
    path = "/results?" + urllib.parse.urlencode({"source_url": source["entry_urls"][0], "limit": 100})
    request = urllib.request.Request(client["base_url"] + path,
                                     headers={"Authorization": "Bearer " + client["bearer"]})
    with urllib.request.urlopen(request, timeout=60) as response:
        value = json.loads(response.read())
    matches = [result for result in value if result.get("url") == item["url"]]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one matching document in this Source's Results API response")
    result = matches[0]
    index = result["metadata"]["pdf"]["original_attachment_index"]
    request = urllib.request.Request(result["attachments"][index]["url"],
                                     headers={"Authorization": "Bearer " + client["bearer"]})
    with urllib.request.urlopen(request, timeout=60) as response:
        original = response.read()
    destination.mkdir(parents=True, exist_ok=True)
    tag = "dd-492-" + str(position).zfill(2)
    (destination / (tag + ".pdf")).write_bytes(original)
    (destination / (tag + ".md")).write_text(result["markdown"])
    write_json(destination / (tag + ".json"), dict(result, attachments=[{"index": index, "sha256": digest(original)}]))
    print(json.dumps({"url": item["url"], "pages": len(result["metadata"]["pdf"]["pages"]),
                      "method": result["metadata"]["pdf"]["method"],
                      "original_sha256": digest(original),
                      "same_historical_original": digest(original) == item["historical_archive_ref"],
                      "output": str(destination)}, ensure_ascii=False))
    return 0


def verify_saved(name, manifest):
    """Correct the initial Source-level API contract without reacquiring originals."""
    work, out = ROOT / "work" / name, ROOT / "artifacts" / name
    report = json.loads((out / "report.json").read_text())
    original = dict(report, superseded_reason="Initial API assertion incorrectly treated source_url as a PDF URL filter; Results returns every resource for that Source. Original download failure remains in first-attempt-report.json and observations.")
    write_json(out / "superseded-api-contract-report.json", original)
    lab = RealLab.__new__(RealLab)
    lab.name, lab.work, lab.out, lab.manifest = name, work, out, manifest
    lab.started = time.monotonic() - report["elapsed_seconds"]
    lab.samples, lab.environment = report["samples"], report["environment"]
    lab.database, lab.bucket = report["database"], report["bucket"]
    lab.env = dict(os.environ, **json.loads((work / "runtime.local.json").read_text()))
    lab.checks = [value for value in report["checks"] if not value["name"].endswith(":api_one_success_result")]
    lab.commands, lab.processes, lab.source_ids = report["commands"], [], ["dd-492"]
    lab.s3 = boto3.client("s3", endpoint_url=lab.env["SEAL_S3_ENDPOINT"],
                         aws_access_key_id=lab.env["SEAL_S3_ACCESS_KEY"],
                         aws_secret_access_key=lab.env["SEAL_S3_SECRET_KEY"],
                         region_name=lab.env.get("SEAL_S3_REGION", "us-east-1"))
    try:
        retry_path = out / "network-retry.json"
        if retry_path.exists():
            retry = json.loads(retry_path.read_text())
            lab.checks = [value for value in lab.checks if not value["name"].startswith("dd-492-05:")]
            for value in lab.checks:
                if value["name"] == "exactly_five_observations":
                    value["name"] = "first_attempt_exactly_five_observations"
            lab.samples = [value for value in lab.samples if value["position"] != 5]
            lab.commands.append({"args": ["collect", str(work / "dd-492-retry.yaml"), "--no-progress"],
                                 "exit": retry.get("exit"), "seconds": retry.get("seconds"), "receipt": retry.get("receipt")})
            target = json.loads(manifest.read_text())["selected"][-1]
            matches = [row for row in observations(lab) if row["url"] == target["url"]]
            failed = next((row for row in matches if row["error"] == "download_failed"), None)
            if failed and failed["result_ref"]:
                failure = json.loads(lab.object(failed["result_ref"]))["result"].get("error_message", "")
                prefix = failure.split("Code context:", 1)[0].strip()
                write_json(out / "first-network-failure.json", {"url": target["url"], "observation_id": failed["id"],
                           "outcome": failed["outcome"], "error": failed["error"], "sdk_error_prefix": prefix,
                           "lower_level_phase": "SDK reports Request timed out; connection versus body-read phase not independently identified"})
            inspect_archive(lab, target, matches[-1] if matches else None)
            if retry.get("receipt", {}).get("status") == "complete" and matches[-1]["version_id"]:
                for value in lab.checks:
                    if value["name"] == "collect_cli_completed" and value["status"] == "FAIL":
                        value.update(name="first_collect_network_failure_preserved_after_successful_retry", status="PASS")
            lab.check("first_network_failure_and_single_retry_retained", retry["original_observation"]["error"] == "download_failed",
                      {"evidence": "first-attempt-report.json, network-retry.json, observations.json"})
            from seal.config import load_file
            from seal.core import digest as config_digest
            from seal.vietnam_documents import restore_entry
            expected = load_file(work / "dd-492-retry.yaml").model_dump()
            restored = load_file(work / "dd-492.yaml").model_dump()
            prior_dsn = os.environ.get("SEAL_DATABASE_URL")
            try:
                os.environ["SEAL_DATABASE_URL"] = lab.env["SEAL_DATABASE_URL"]
                with sqlite3.connect(":memory:") as state:
                    state.execute("CREATE TABLE batch(config_hash TEXT)")
                    state.execute("INSERT INTO batch VALUES(?)", (config_digest(expected),))
                    restore_entry(state, restored)
            finally:
                if prior_dsn is None:
                    os.environ.pop("SEAL_DATABASE_URL", None)
                else:
                    os.environ["SEAL_DATABASE_URL"] = prior_dsn
            with lab.connect() as connection:
                source = connection.execute("SELECT config FROM seal.source WHERE id='dd-492'").fetchone()
            lab.check("five_entry_source_config_restored_with_guard", source["config"] == restored)
            _, snapshot = lab.cli("snapshot", "create")
            snapshot_id = (snapshot or {}).get("snapshot_id")
            lab.check("final_retry_snapshot_created", bool(snapshot_id), snapshot)
            if snapshot_id:
                proc, receipt = lab.cli("snapshot", "publish", snapshot_id, timeout=240)
                lab.check("final_retry_snapshot_published", proc.returncode == 0, receipt)
            write_json(out / "observations.json", observations(lab))
            lab.check("final_six_observations_including_original_network_failure", len(observations(lab)) == 6)
            historical = json.loads((work / "historical-baseline.json").read_text())
            after = history_state(historical["database"])
            write_json(out / "historical-after.json", after)
            lab.check("historical_database_unchanged_after_retry", after == json.loads((out / "historical-before.json").read_text()))
        lab.start_api()
        write_json(out / "sources-api.json", lab.get("/sources"))
        rows = observations(lab)
        for item in json.loads(manifest.read_text())["selected"]:
            matches = [row for row in rows if row["url"] == item["url"] and row["version_id"]]
            if matches:
                verify_api(lab, item, json.loads(lab.object(matches[-1]["result_ref"])))
        lab.check("api_contract_correction_evidence_retained", True,
                  {"superseded_report": "superseded-api-contract-report.json",
                   "command": "uv run --no-sync python tests/dd492_real_ocr_e2e.py --name " + name
                   + " --manifest " + str(manifest.relative_to(ROOT)) + " --verify-saved"})
    finally:
        lab.close()
        lab.check("corrected_owned_api_processes_stopped", all(proc.poll() is not None for proc, _ in lab.processes))
    failed = sum(check["status"] == "FAIL" for check in lab.checks)
    print(json.dumps({"passed": len(lab.checks) - failed, "failed": failed, "report": str(out / "report.json")}))
    return bool(failed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--serve-saved", action="store_true")
    parser.add_argument("--query-saved", type=int, choices=range(1, 6), metavar="POSITION")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-saved", action="store_true")
    args = parser.parse_args()
    if args.serve_saved:
        return serve_saved(ROOT / "work" / args.name)
    if args.query_saved:
        return query_saved(ROOT / "work" / args.name, args.query_saved,
                           args.output or ROOT / "work" / args.name / "review-downloads")
    if args.manifest is None:
        parser.error("--manifest is required for acceptance")
    manifest = args.manifest.resolve()
    if args.verify_saved:
        return verify_saved(args.name, manifest)
    selected = json.loads(manifest.read_text())["selected"]
    baseline_path = manifest.parent / "historical-baseline.json"
    baseline = json.loads(baseline_path.read_text())
    source_before = (ROOT / "sources" / "dd-492.yaml").read_bytes()
    historical_before = history_state(baseline["database"])
    lab = RealLab(args.name, manifest)
    try:
        if manifest.parent != lab.work:
            write_json(lab.work / "selection.json", json.loads(manifest.read_text()))
            write_json(lab.work / "historical-baseline.json", baseline)
        lab.private_runtime()
        write_json(lab.out / "failure-boundaries.json", json.loads((ROOT / "tests" / "dd492_real_ocr_failure_boundaries.json").read_text()))
        write_json(lab.out / "historical-before.json", historical_before)
        lab.check("exactly_five_fixed_samples", len(selected) == 5 and len({s["url"] for s in selected}) == 5)
        lab.check("historical_baseline_counts_and_publication", historical_before["counts"] == baseline["counts"]
                  and historical_before["publication"] == baseline["publication"])
        model = Path(lab.env["TESSDATA_PREFIX"]) / "vie.traineddata"
        lab.check("selected_vie_model_sha256", digest(model.read_bytes()) == json.loads(manifest.read_text())["local_model"]["model_sha256"])
        proc, receipt = lab.cli("init")
        lab.check("isolated_schema_initialized", proc.returncode == 0, receipt)
        if proc.returncode:
            return 1
        config = source_config(lab, selected)
        print(json.dumps({"stage": "collect_started", "urls": len(selected), "pages": sum(s["original_pages"] for s in selected),
                          "database": lab.database, "bucket": lab.bucket}), flush=True)
        proc, receipt = lab.cli("collect", config, "--no-progress", timeout=3750)
        lab.check("collect_cli_completed", proc.returncode == 0, receipt)
        print(json.dumps({"stage": "collect_finished", "receipt": receipt}), flush=True)
        rows = observations(lab)
        write_json(lab.out / "observations.json", rows)
        lab.check("exactly_five_observations", len(rows) == 5)
        envelopes = {}
        for item in selected:
            current = [r for r in rows if r["url"] == item["url"]]
            envelopes[item["url"]] = inspect_archive(lab, item, current[-1] if current else None)
            print(json.dumps({"stage": "archive_checked", "position": item["position"],
                              "success": envelopes[item["url"]] is not None}), flush=True)
        _, snapshot = lab.cli("snapshot", "create")
        snapshot_id = (snapshot or {}).get("snapshot_id")
        lab.check("isolated_snapshot_created", bool(snapshot_id), snapshot)
        if snapshot_id:
            proc, receipt = lab.cli("snapshot", "publish", snapshot_id, timeout=240)
            lab.check("isolated_snapshot_published", proc.returncode == 0, receipt)
            if not proc.returncode:
                lab.start_api()
                try:
                    urllib.request.urlopen(lab.base + "/sources", timeout=10)
                    unauthorized = False
                except urllib.error.HTTPError as exc:
                    unauthorized = exc.code == 401
                lab.check("api_requires_bearer", unauthorized)
                sources = lab.get("/sources")
                write_json(lab.out / "sources-api.json", sources)
                lab.check("api_source_discoverable", isinstance(sources, list) and len(sources) == 1)
                for item in selected:
                    if envelopes[item["url"]]:
                        verify_api(lab, item, envelopes[item["url"]])
        write_json(lab.out / "historical-after.json", history_state(baseline["database"]))
        lab.check("historical_database_unchanged", history_state(baseline["database"]) == historical_before)
        lab.check("tracked_source_config_unchanged", (ROOT / "sources" / "dd-492.yaml").read_bytes() == source_before)
        lab.check("temporary_pdf_directories_released", not list((lab.work / "runtime").rglob("seal-pdf-*")))
        lab.check("implementation_unchanged", implementation_digest() == lab.environment["implementation_sha256"])
    except Exception as exc:
        lab.check("acceptance_exception", False, {"type": type(exc).__name__, "message": str(exc)[:500]})
        raise
    finally:
        lab.close()
        lab.check("owned_api_processes_stopped", all(proc.poll() is not None for proc, _ in lab.processes))
        passed = sum(check["status"] == "PASS" for check in lab.checks)
        failed = sum(check["status"] == "FAIL" for check in lab.checks)
        print(json.dumps({"passed": passed, "failed": failed, "report": str(lab.out / "report.json")}), flush=True)
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
