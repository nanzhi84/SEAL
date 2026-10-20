"""Fresh #31 supplemental E2E: rendered DOM, OCR quality routes and transport facts.

Run: uv run --no-sync python tests/issue31_supplement.py
All storage and source services are isolated; no product module is imported.
Failure boundaries, established before the scenarios:
1. A JS-only paragraph is absent from archived rendered DOM or delivered Markdown.
2. Invalid page mappings, missing scan/native text, replacement characters, legacy
   font garbage, empty/flattened tables or invented blank-page text become success.
3. Low-ink blank pages, unstable reading order or absent signature blocks lose their
   explicit accept_with_warning route, or are silently described as clean success.
4. Accepted results cannot seal, publish, and download byte-identical originals/MD.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from issue31_acceptance import file_hashes
from issue31_cases_formats import test_init_new_contract_schema
from issue31_common import S, collect, current_rows, doc_json, failure_evidence, md
from issue31_fixtures import MIXED_NATIVE_LINES, Content, br_mixed_result
from issue31_support import (
    ROOT, Api, BrOcrMock, Fixture, Lab, PdfBuilder, base_source, br_result,
    html_page, run_tests, scanned_page_jpeg, sha256,
)
from issue31_cases_transport import (
    test_byte_budget_no_partial_archive, test_document_budget,
    test_external_wait_has_no_long_transaction, test_redirect_scope_facts,
    test_retry_facts, test_robots_prevents_request,
)


def test_rendered_dom(lab):
    """CLI archives DOM UTF-8 and delivers content created only by JavaScript."""
    fx = Fixture()
    S["dom_fixture"] = fx
    marker = "DOM EXECUTION VERIFIED PUBLIC DOCUMENT CONTENT"
    page = html_page("Rendered Evidence", "<p id='target'></p><script>"
                     f"document.querySelector('#target').textContent='{marker}';</script>")
    fx.put("/dynamic", page)
    cfg = base_source("dd-247", fx, ["/dynamic"], fetch={"mode": "rendered_dom"})
    result, receipt, _ = collect(lab, "dd-247", cfg, label="rendered-dom", timeout=120)
    lab.save("receipts/rendered-dom.json", receipt or result.stdout + result.stderr)
    lab.check("rendered collect exits 0", result.returncode == 0, result.stderr[-1200:])
    rows = current_rows(lab, "dd-247")
    lab.check("one rendered document", len(rows) == 1, rows)
    row = rows[0]
    body = md(lab, row["markdown_hash"])
    lab.check("JS-only text delivered", marker in body, body)
    acq = lab.q("SELECT acquisition_kind,representation,body_hash FROM seal.acquisition WHERE source_id='dd-247' AND state='archived'")
    lab.check("rendered_dom acquisition recorded", any(a["acquisition_kind"] == "rendered_dom" for a in acq), acq)
    raw = lab.s3_get(row["original_hash"])
    lab.check("CAS rendered DOM contains rendered paragraph", (f'<p id="target">{marker}</p>').encode() in raw, raw.decode()[:1600])
    lab.check("DOM original is UTF-8 HTML", raw.decode("utf-8").strip().startswith("<"), row["original_hash"])


def setup_quality(lab):
    """Prepare a single local OCR service and public PDF fixture server."""
    S["quality_fixture"] = Fixture()
    S["quality_mock"] = BrOcrMock()


def _quality_case(lab, name, payload, route, reason, pdf=None):
    fx, mock = S["quality_fixture"], S["quality_mock"]
    original = pdf or S["content"].b["mixed.pdf"]
    path = f"/quality/{name}.pdf"
    fx.put(path, original, "application/pdf")
    mock.result_factory = lambda req, raw: payload
    cfg = base_source("dd-058", fx, [path], metadata={"language": "vi"},
                      ocr={"engine": "br-ocr-v1", "model": "acceptance-v1", "timeout_seconds": 30, "poll_seconds": .1})
    result, receipt, _ = collect(lab, "dd-058", cfg, label=name, timeout=90,
                                 env_extra={"SEAL_BR_OCR_URL": mock.base})
    lab.save(f"receipts/{name}.json", receipt or result.stdout + result.stderr)
    rows = [r for r in current_rows(lab, "dd-058") if r["public_url"] == fx.base + path]
    if route in {"vision_reparse_pages", "legacy_font_encoding_recovery"}:
        evidence = failure_evidence(lab, "dd-058", fx.base + path)
        lab.save(f"evidence/{name}-failure.json", evidence)
        lab.check(f"{name}: no success row", not rows, rows)
        lab.check(f"{name}: explicit quality route", route in json.dumps(evidence, default=str), evidence)
        return
    lab.check(f"{name}: one warning delivery", len(rows) == 1, rows)
    document = doc_json(lab, rows[0]["result_ref"])
    report = document["deliverables"][0]["quality_report"]
    lab.save(f"evidence/{name}-quality.json", report)
    lab.check(f"{name}: expected route", report.get("recommended_route") == route, report)
    lab.check(f"{name}: warning reason recorded", reason in json.dumps(report), report)


def _payload(blocks):
    return br_result([[{"type": "text", "text": line} for line in MIXED_NATIVE_LINES], blocks])


def scenarios():
    normal = "SCANNED PAGE TWO CONTENT INCLUDING ITS SIGNATURE AND PUBLIC OBLIGATIONS."
    cases = []
    cases.append(("replacement", _payload([{"type": "text", "text": normal + " \ufffd\ufffd\ufffd"}]),
                  "vision_reparse_pages", "replacement_character", None))
    cases.append(("legacy_garbage", _payload([{"type": "text", "text": normal + " §µ¶¸¹©®§µ¶"}]),
                  "legacy_font_encoding_recovery", "legacy", None))
    cases.append(("foreign_garbage", _payload([{"type": "text", "text": normal + "中文乱码内容没有任何越南文上下文"}]),
                  "vision_reparse_pages", "non_vietnamese_garbage", None))
    cases.append(("scan_loss", _payload([{"type": "text", "text": "Tiny"}]),
                  "vision_reparse_pages", "scan_page_with_little_ocr_text", None))
    cases.append(("table_empty", _payload([{"type": "text", "text": normal}, {"type": "table", "table_body": ""}]),
                  "vision_reparse_pages", "table_content_missing", None))
    flat = _payload([{"type": "text", "text": normal}])
    parsed = json.loads(flat["page_parse_json"])
    parsed.append({"page_idx": 1, "type": "table", "text": "Undelimited cell value " * 12, "bbox": [10, 300, 500, 700]})
    flat["page_parse_json"] = json.dumps(parsed)
    cases.append(("table_flat", flat, "vision_reparse_pages", "table_structure_missing", None))
    mapping = br_mixed_result("WRONG-PAGE-MAPPING")
    parsed = json.loads(mapping["page_parse_json"])
    for block in parsed:
        block["page_idx"] += 10
    mapping["page_parse_json"] = json.dumps(parsed)
    cases.append(("mapping", mapping, "legacy_font_encoding_recovery", "page_number_mapping_unreliable", None))
    image, size = scanned_page_jpeg([normal])
    native = PdfBuilder().page(["NATIVE PAGE LONG BUSINESS DOCUMENT CONTENT. " * 7]).page(
        image_jpeg=image, image_size=size).build()
    loss = br_result([[{"type": "text", "text": "Missing"}], [{"type": "text", "text": normal}]])
    cases.append(("native_loss", loss, "vision_reparse_pages", "native_text_page_with_little_ocr_text", native))
    signature = _payload([{"type": "text", "text": normal + " NƠI NHẬN: Cơ quan công bố nhận thông báo."}])
    cases.append(("signature_warning", signature, "accept_with_warning", "signature_block_missing", None))
    order = [{"type": "text", "text": f"Paragraph {i} explains public reporting obligations.",
              "bbox": [10, 600 if i % 2 == 0 else 100, 500, 650 if i % 2 == 0 else 150]} for i in range(8)]
    cases.append(("reading_order_warning", _payload(order), "accept_with_warning", "reading_order_unstable", None))
    blank = PdfBuilder().page(MIXED_NATIVE_LINES).page().page(image_jpeg=image, image_size=size).build()
    blank_payload = br_result([[{"type": "text", "text": line} for line in MIXED_NATIVE_LINES], [],
                               [{"type": "text", "text": normal}]])
    cases.append(("blank_warning", blank_payload, "accept_with_warning", "low_ink_blank_page_candidate", blank))
    spurious = br_result([[{"type": "text", "text": line} for line in MIXED_NATIVE_LINES],
                          [{"type": "text", "text": normal * 2}], [{"type": "text", "text": normal}]])
    cases.append(("blank_spurious", spurious, "vision_reparse_pages", "blank_page_with_spurious_text", blank))
    functions = []
    for case in cases:
        def check(lab, case=case):
            return _quality_case(lab, *case)
        check.__name__ = "test_quality_" + case[0]
        check.__doc__ = f"Real br-ocr CLI routes {case[0]} to {case[2]}."
        functions.append(check)
    return functions


def test_publish_and_api_bytes(lab):
    """Accepted DOM/OCR warnings seal, publish and download through both file hashes."""
    result, snapshot = lab.cli_json("snapshot", "create")
    lab.check("supplement snapshot sealed", result.returncode == 0 and snapshot.get("sealed"), result.stdout[-800:])
    result, _ = lab.cli_json("snapshot", "publish", snapshot["snapshot_id"])
    lab.check("supplement snapshot published", result.returncode == 0, result.stderr[-800:])
    api = Api(lab, {"all": ["dd-247", "dd-058"]}).start()
    try:
        items, _, _ = api.all_results("all")
        lab.check("accepted rows published", len(items) >= 3, len(items))
        comparisons = []
        for row in items:
            for key in ("original_hash", "markdown_hash"):
                digest = row[key]
                if not digest:
                    continue
                response = api.get("/files/" + digest, "all")
                comparison = {"kind": key, "sha256": digest, "http": response.status_code,
                              "api_sha256": sha256(response.content), "cas_sha256": sha256(lab.s3_get(digest))}
                comparisons.append(comparison)
                lab.check("published supplement byte identity", response.status_code == 200 and comparison["api_sha256"] == digest,
                          comparison)
        lab.save("evidence/supplement-api-results.json", items)
        lab.save("evidence/supplement-byte-comparison.json", comparisons)
    finally:
        api.stop()


def main():
    lab = Lab("issue31-supplement")
    print("artifacts:", lab.artifacts, flush=True)
    start, hashes = file_hashes()
    lab.save("failure-boundaries.txt", __doc__)
    lab.save("environment.json", {"python": sys.version, "src_seal_tree_sha256": start,
                                  "file_sha256": hashes, "reproduce": "uv run --no-sync python tests/issue31_supplement.py",
                                  "isolation": "New throwaway PostgreSQL/MinIO containers, random ports, local fixtures; no production data",
                                  "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()})
    try:
        lab.start()
        S["content"] = Content(lab.work / "fixtures")
        tests = [test_init_new_contract_schema, test_rendered_dom, setup_quality, *scenarios(),
                 test_retry_facts, test_redirect_scope_facts, test_robots_prevents_request,
                 test_byte_budget_no_partial_archive, test_document_budget,
                 test_external_wait_has_no_long_transaction, test_publish_and_api_bytes]
        run_tests(lab, tests)
    finally:
        end, end_hashes = file_hashes()
        lab.save("source-at-end.json", {"src_seal_tree_sha256": end, "file_sha256": end_hashes})
        summary = {s: sum(r["status"] == s for r in lab.results) for s in ("PASS", "FAIL", "NOT VERIFIED")}
        lab.save("summary.json", {"summary": summary, "results": lab.results, "source_changed_during_run": start != end})
        lab.stop()
        for key in ("dom_fixture", "quality_fixture", "quality_mock"):
            if key in S:
                S[key].close()
        print(json.dumps(summary), flush=True)
    return bool(summary["FAIL"] or summary["NOT VERIFIED"])


if __name__ == "__main__":
    raise SystemExit(main())
