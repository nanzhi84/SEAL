"""Issue 25 business acceptance: real acquisition -> storage -> Snapshot -> HTTP API.

Run from repository root: uv run --no-sync python tests/issue25_e2e.py
No OCR output is mocked. Test fixtures contain synthetic public-safe text only.
"""

import argparse
import json
import time
import urllib.parse

from issue25_fixtures import FixtureServer, fixtures
from issue25_lab import Lab, digest, implementation_digest


EXPECTED = {
    "native": ("native", [True, False, True]),
    "scan": ("ocr", [True, False, True]),
    "redirect_scan": ("ocr", [True, False, True]),
    "mixed": ("native", [True, False]),
    "page_number_only": ("native", [True]),
}
ERRORS = {
    "all_empty": "pdf_no_text", "corrupt": "pdf_invalid_or_encrypted",
    "encrypted": "pdf_invalid_or_encrypted", "zero_pages": "pdf_zero_pages",
}


def pdf_assertions(lab, prefix, metadata, method, nonempty):
    pdf = (metadata or {}).get("pdf")
    lab.check(prefix + ":method", isinstance(pdf, dict) and pdf.get("method") == method)
    pages = pdf.get("pages") if isinstance(pdf, dict) else None
    valid = isinstance(pages, list) and len(pages) == len(nonempty)
    lab.check(prefix + ":page_count", valid, {"expected": len(nonempty), "actual": len(pages) if isinstance(pages, list) else None})
    if not valid:
        return
    lab.check(prefix + ":original_page_numbers", [p.get("page_number") for p in pages] == list(range(1, len(pages) + 1)))
    lab.check(prefix + ":page_text_and_empty_position", [bool(p.get("text", "").strip()) for p in pages] == nonempty)
    lab.check(prefix + ":no_invented_blank_classification", all(p.get("status") != "blank" and p.get("blank") is not True for p in pages))
    if method == "ocr":
        serialized = json.dumps(pdf, ensure_ascii=False)
        lab.check(prefix + ":engine_recorded", "tesseract" in serialized.lower())
        lab.check(prefix + ":multilingual_config_recorded", all(lang in serialized for lang in ["eng", "chi_sim", "vie"]))
    if prefix.startswith("api"):
        lab.check(prefix + ":no_private_paths", "/Users/" not in json.dumps(pdf) and "input.pdf" not in json.dumps(pdf))


def observations(lab, source_id):
    with lab.connect() as conn:
        return conn.execute("""
            SELECT o.id,o.outcome,o.error,o.body_hash,o.archive_ref,o.result_ref,o.version_id,
                   r.url,r.current_version_id,v.result_ref AS version_result_ref
            FROM seal.observation o
            JOIN seal.resource r ON r.id=o.resource_id
            JOIN seal.frontier f ON f.id=r.frontier_id
            LEFT JOIN seal.version v ON v.id=o.version_id
            WHERE f.source_id=%s ORDER BY o.id
        """, (source_id,)).fetchall()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="issue25-real-" + time.strftime("%Y%m%d_%H%M%S"))
    args = parser.parse_args()
    lab = Lab(args.name)
    try:
        data = fixtures(lab.work / "inputs")
        proc, receipt = lab.cli("init")
        lab.check("isolated_schema_initialized", proc.returncode == 0, receipt)
        if proc.returncode:
            return 1
        rows_by_name = {}
        with FixtureServer(data) as server:
            for number, name in enumerate(data, 1):
                source_id = "dd-" + str(number).zfill(3)
                url = server.base + "/" + name + ".pdf"
                source = lab.source(source_id, [url])
                before_scanned_gets = server.requests["/scan.pdf"]
                proc, receipt = lab.cli("collect", source, "--no-progress")
                rows = observations(lab, source_id)
                lab.check(name + ":single_download", server.requests["/" + name + ".pdf"] == 1,
                          {"GETs": server.requests["/" + name + ".pdf"]})
                if name == "redirect_scan":
                    lab.check(name + ":single_final_pdf_download", server.requests["/scan.pdf"] == before_scanned_gets + 1)
                lab.check(name + ":observation_present", len(rows) == 1, {"count": len(rows)})
                rows_by_name[name] = {"source_id": source_id, "url": url, "rows": rows}
                if not rows:
                    continue
                row = rows[-1]
                original_hash = digest(data[name])
                lab.check(name + ":original_body_hash", row["body_hash"] == original_hash)
                lab.check(name + ":original_archive_ref", row["archive_ref"] == original_hash)
                if row["archive_ref"]:
                    lab.check(name + ":original_s3_bytes", lab.object(row["archive_ref"]) == data[name])
                envelope = json.loads(lab.object(row["result_ref"])) if row["result_ref"] else None
                lab.check(name + ":full_result_archived", isinstance(envelope, dict) and isinstance(envelope.get("result"), dict))
                if name in EXPECTED:
                    method, nonempty = EXPECTED[name]
                    lab.check(name + ":successful_version", row["outcome"] == "parsed" and bool(row["version_id"]) and row["current_version_id"] == row["version_id"])
                    lab.check(name + ":version_same_result", bool(row["version_result_ref"]) and row["version_result_ref"] == row["result_ref"])
                    if envelope:
                        result = envelope["result"]
                        pdf_assertions(lab, "archive:" + name, result.get("metadata"), method, nonempty)
                        markdown = result.get("markdown") or {}
                        text = markdown.get("raw_markdown", "") if isinstance(markdown, dict) else markdown
                        lab.check(name + ":html_and_markdown_nonempty", bool((result.get("cleaned_html") or "").strip()) and bool(text.strip()))
                        lab.check(name + ":same_original_reference", original_hash in envelope.get("attachment_refs", []))
                        if name in {"mixed", "page_number_only"}:
                            lab.check(name + ":accepted_missing_scan_body", "SCANNED FIRST PAGE ALPHA" not in text)
                else:
                    lab.check(name + ":stable_failure", row["error"] == ERRORS[name], {"expected": ERRORS[name], "actual": row["error"]})
                    lab.check(name + ":no_successful_version", row["version_id"] is None and row["current_version_id"] is None)

            # A poisoned executable proves native documents do not invoke OCR itself.
            no_ocr = lab.work / "ocr-must-not-run"
            invocation = lab.work / "unexpected-ocr-invocations"
            no_ocr.write_text("#!/bin/sh\nprintf called >> '" + str(invocation) + "'\nexit 93\n")
            no_ocr.chmod(0o700)
            for number, name in enumerate(["native", "mixed", "page_number_only"], 20):
                source = lab.source("dd-" + str(number).zfill(3), [server.base + "/poison/" + name + ".pdf"])
                env = dict(lab.env, SEAL_TESSERACT=str(no_ocr))
                _, receipt = lab.cli("collect", source, "--no-progress", env=env)
                rows = observations(lab, "dd-" + str(number).zfill(3))
                lab.check(name + ":ocr_not_called", len(rows) == 1 and rows[0]["version_id"] is not None and not invocation.exists())

            _, snapshot = lab.cli("snapshot", "create")
            snapshot_id = (snapshot or {}).get("snapshot_id")
            lab.check("snapshot_created", bool(snapshot_id))
            if not snapshot_id:
                return 1
            proc, published = lab.cli("snapshot", "publish", snapshot_id)
            lab.check("snapshot_published", proc.returncode == 0, published)
            lab.start_api()
            discovered = lab.get("/sources")
            lab.check("sources_api_available", isinstance(discovered, list))
            for name, details in rows_by_name.items():
                results = lab.get("/results?" + urllib.parse.urlencode({"source_url": details["url"], "limit": 100}))
                if name not in EXPECTED:
                    lab.check("api:" + name + ":no_failed_content", results == [])
                    continue
                lab.check("api:" + name + ":one_success_result", isinstance(results, list) and len(results) == 1)
                if len(results) != 1:
                    continue
                result = results[0]
                method, nonempty = EXPECTED[name]
                pdf_assertions(lab, "api:" + name, result.get("metadata"), method, nonempty)
                lab.check("api:" + name + ":markdown", isinstance(result.get("markdown"), str) and bool(result["markdown"].strip()))
                attachments = result.get("attachments", [])
                original_index = result.get("metadata", {}).get("pdf", {}).get("original_attachment_index")
                expected_attachments = 2 if name == "redirect_scan" else 1
                valid_index = type(original_index) is int and 0 <= original_index < len(attachments)
                lab.check("api:" + name + ":original_retrievable", len(attachments) == expected_attachments and valid_index)
                if valid_index:
                    attachment = lab.get(attachments[original_index]["url"], binary=True)
                    lab.check("api:" + name + ":original_hash", digest(attachment) == digest(data[name]))
                # Capabilities and bearer secrets are not copied to artifacts.
                safe = dict(result, attachments=[{"sha256": digest(data[name]), "count": len(attachments)}])
                (lab.out / (name + "-api.json")).write_text(json.dumps(safe, ensure_ascii=False, indent=2))

        lab.check("temporary_pdf_directories_released", not list((lab.work / "runtime").rglob("seal-pdf-*")))
        lab.check("implementation_frozen_during_acceptance", lab.environment["implementation_sha256"] == implementation_digest())
        passed = sum(c["status"] == "PASS" for c in lab.checks)
        failed = sum(c["status"] == "FAIL" for c in lab.checks)
        print(json.dumps({"passed": passed, "failed": failed, "report": str(lab.out / "report.json")}))
        return bool(failed)
    finally:
        lab.close()


if __name__ == "__main__":
    raise SystemExit(main())
