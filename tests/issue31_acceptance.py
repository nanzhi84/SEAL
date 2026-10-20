"""Issue #31 (原生 Source Adapter 1/4) independent end-to-end business acceptance.

Run from the repository root:

    uv run --no-sync python tests/issue31_acceptance.py

Everything is exercised from the outside: the real ``seal`` CLI in subprocesses,
throwaway PostgreSQL/MinIO containers (random ports and credentials), loopback
fixture HTTP sources (``SEAL_ALLOW_LOOPBACK=1``), a controllable ``br-ocr-v1``
mock, SQL rows in the ``seal`` schema, CAS objects via the S3 API and the HTTP
API started with ``seal serve``.  Runtime data: ``work/issue31-<stamp>/``;
evidence: ``artifacts/issue31-<stamp>/``.

Failure boundaries (defined before writing the assertions)
----------------------------------------------------------
B1  Chain: a supported input (HTML, TXT, DOCX, DOC, RTF, XLS/XLSX, disguised
    spreadsheet, ZIP, native PDF, mixed scanned PDF, TCVN3 RTF/DOC) fails to
    reach Acquisition -> CAS -> Version -> delivery row -> Snapshot -> API, or its
    Markdown loses the source text / page order.
B2  CAS: an archived Acquisition or delivered file whose S3 object is missing or
    whose bytes do not hash to the recorded SHA-256; a delivery row whose
    ``markdown_hash`` object is absent.
B3  Office: a spreadsheet disguised as .doc/.docx is rejected or handed to a
    word processor; a ZIP is not unpacked into its inner documents; RTF control
    groups (fonttbl/stylesheet/pict/header/footer) leak into the text; DOCX
    merged cells are duplicated.
B4  TCVN3: legacy-font lines stay garbled, already-Unicode header/date lines are
    corrupted, or Phần/Chương/Mục/Điều heading levels are not restored.
B5  PDF/OCR: a scanned page whose only native text is a tiny signature string is
    not OCR'd (text missing); br-ocr-v1 result is not used; a scanned page with
    almost no OCR text is accepted as a successful Version.
B6  Rows: single file not 1 row with empty 文件顺序; consecutive volumes not
    split into 1..N rows sharing 资料名称/来源记录号 with ``_01``/``_02`` names
    in label order (not page order); independent attachments not own rows with
    own 资料名称, shared 来源记录号, empty 文件顺序; ambiguous volume order not
    blocked (any row produced).
B7  Identity/versioning: same-named originals in different documents or the
    same official number in different sources share a Resource/来源记录号;
    listing re-order creates a version; a real input change or a parser
    change does not; A->B->A collapses to fewer than three versions; same input
    + same parser but different output silently becomes a new version.
B8  API: /sources or /attachments/* answer anything but 404; /results misses
    or reorders the 16 template columns, misses markdown_hash/source_id/
    resource_id/version_id, breaks pagination/cursor integrity or ignores
    authorisation; /files returns bytes whose SHA-256 differs from the request,
    or serves another source's / unpublished / internal (inputs manifest,
    unified document JSON, listing page, failed-document original) objects.
B9  Failures: 404 attachment, refused/reset connection, quality-gate failure,
    SIGINT/SIGTERM cancellation and stale (killed) attempts leave no explicit
    failure, produce a success Version/delivery row, or leave ``running``
    acquisitions; a snapshot can be sealed while a referenced object is
    missing or corrupt.
"""

# ruff: noqa: F403,F405
from issue31_common import *
from issue31_cases_formats import *
from issue31_cases_tcvn3 import *
from issue31_cases_ocr import *
from issue31_cases_rows import *
from issue31_cases_versions import *
from issue31_cases_api import *
from issue31_cases_faults import *

# ============================================================ main


TESTS = [
    test_init_new_contract_schema,
    test_collect_formats_chain,
    test_format_html, test_format_txt, test_format_docx, test_format_doc_libreoffice, test_format_rtf,
    test_format_xlsx, test_format_xls, test_format_disguised_xlsx_as_docx, test_format_disguised_xls_as_doc,
    test_format_zip_with_documents, test_format_native_pdf,
    test_collect_tcvn3, test_tcvn3_rtf_mixed_encoding, test_tcvn3_doc_mixed_encoding, test_tcvn3_unicode_header_with_overlapping_glyph,
    test_collect_scan_tesseract, test_scan_signature_page_ocr_tesseract,
    test_scan_no_text_document_fails, test_scan_quality_gate_blocks_page_without_ocr_text,
    test_collect_scan_br_ocr,
    test_collect_structure, test_rows_single_file, test_rows_consecutive_volumes, test_rows_independent_attachments,
    test_rows_ambiguous_volume_duplicate_blocked, test_rows_ambiguous_volume_gap_blocked,
    test_failure_attachment_404, test_failure_attachment_connection_reset,
    test_identity_same_named_originals, test_listing_page_not_deliverable, test_identity_same_number_different_sources,
    test_version_first_collect,
    test_cas_integrity_formats,
    test_api_before_publish_503, test_snapshot_create_and_publish,
    test_api_removed_endpoints, test_api_authentication, test_api_results_16_columns, test_api_pagination_and_cursor,
    test_api_files_bytes, test_api_files_forbidden,
    test_version_listing_reorder_unchanged, test_version_real_change_and_unpublished_hash, test_version_a_b_a_three_versions,
    test_version_parser_change, test_nondeterministic_output_conflict,
    test_failure_network_refused_entry, test_cancel_sigint, test_cancel_sigterm, test_stale_attempt_takeover,
    test_corrupt_markdown_object_blocks_snapshot, test_missing_original_object_blocks_snapshot,
    test_missing_inputs_manifest_blocks_snapshot, test_snapshot_ok_after_restore,
]


def file_hashes():
    out = {}
    for path in sorted((ROOT / "src" / "seal").rglob("*.py")) + sorted((ROOT / "tests").glob("issue31_*.py")) + [ROOT / "pyproject.toml", ROOT / "uv.lock"]:
        out[str(path.relative_to(ROOT))] = sha256(path.read_bytes())
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src" / "seal").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest(), out


def main():
    only = set(sys.argv[1:])
    lab = Lab("issue31")
    print("work:", lab.work, "\nartifacts:", lab.artifacts, flush=True)
    src_digest, hashes = file_hashes()
    lab.save("failure-boundaries.txt", __doc__)
    lab.save("environment.json", {
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "git_branch": subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, text=True).strip(),
        "git_status_porcelain": subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True),
        "src_seal_tree_sha256": src_digest, "file_sha256": hashes, "python": sys.version,
        "images": {"postgres": "postgres:17-alpine", "minio": "seal-minio:RELEASE.2025-10-15T17-29-55Z"},
        "tesseract": subprocess.run(["tesseract", "--version"], capture_output=True, text=True).stdout.splitlines()[:1],
        "reproduce": "uv run --no-sync python tests/issue31_acceptance.py",
        "isolation": "throwaway containers (tmpfs, random ports/credentials), dedicated bucket, loopback fixtures, br-ocr-v1 mock",
    })
    try:
        lab.start()
        S["content"] = Content(lab.work / "fixtures")
        for name, data in S["content"].b.items():
            p = lab.work / "fixtures" / "served" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        lab.save("evidence/fixture-sha256.json", {k: sha256(v) for k, v in S["content"].b.items()})
        tests = [t for t in TESTS if not only or t.__name__ in only]
        run_tests(lab, tests)
    finally:
        if "api" in S:
            S["api"].stop()
        with __import__("contextlib").suppress(Exception):
            lab.save("evidence/sql-delivery-rows.json", lab.q("SELECT * FROM seal.delivery_row ORDER BY version_id, row_number"))
            lab.save("evidence/sql-acquisitions.json", lab.q("SELECT id, source_id, request_url, state, http_status, outcome, error, body_hash, parent_relation, attempt_epoch FROM seal.acquisition ORDER BY started_at"))
            lab.save("evidence/sql-observations.json", lab.q("SELECT o.*, r.external_key FROM seal.observation o JOIN seal.resource r ON r.id=o.resource_id ORDER BY observed_at"))
            lab.save("evidence/sql-runs.json", lab.q("SELECT id, source_id, mode, status, attempt_epoch, errors, report FROM seal.run ORDER BY created_at"))
        lab.stop()
        summary = {s: sum(1 for r in lab.results if r["status"] == s) for s in ("PASS", "FAIL", "NOT VERIFIED")}
        end_digest, end_hashes = file_hashes()
        lab.save("source-at-end.json", {"src_seal_tree_sha256": end_digest, "file_sha256": end_hashes,
                                       "source_changed_during_run": src_digest != end_digest})
        lab.save("summary.json", {"summary": summary, "results": lab.results,
                                 "source_changed_during_run": src_digest != end_digest})
        print(json.dumps(summary), flush=True)
        print("artifacts:", lab.artifacts, flush=True)
    return 1 if summary["FAIL"] or summary["NOT VERIFIED"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
