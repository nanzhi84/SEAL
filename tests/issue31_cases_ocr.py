"""Independent Issue 31 business scenarios; product access uses CLI, SQL, S3 and HTTP."""
# ruff: noqa: F403,F405
from issue31_common import *

# ============================================================ scanned PDF (dd-040 tesseract, dd-041 br-ocr)


def test_collect_scan_tesseract(lab):
    """Mixed scanned PDF with tesseract; noise scan fails quality gate."""
    c = S["content"]
    fx = Fixture()
    S["fx_scan"] = fx
    fx.put("/scan/mixed.pdf", c.b["mixed.pdf"], "application/pdf")
    fx.put("/scan/noise.pdf", c.b["noise.pdf"], "application/pdf")
    fx.put("/scan/mixed-noise.pdf", c.b["mixed-noise.pdf"], "application/pdf")
    cfg = base_source("dd-040", fx, ["/scan/mixed.pdf", "/scan/noise.pdf", "/scan/mixed-noise.pdf"], ocr={"engine": "tesseract", "languages": "eng", "dpi": 150})
    result, receipt, _ = collect(lab, "dd-040", cfg, label="collect scan tesseract")
    S["receipt_scan"] = receipt
    lab.save("receipts/dd-040-scan.json", receipt or result.stdout)
    lab.check("collect scan returns a receipt", isinstance(receipt, dict), result.stdout[-1500:] + result.stderr[-1500:])


def test_scan_signature_page_ocr_tesseract(lab):
    """Scanned page with only a tiny native signature string is OCR'd (tesseract)."""
    fx = S["fx_scan"]
    row = single_row(lab, "dd-040", fx.base + "/scan/mixed.pdf")
    body = md(lab, row["markdown_hash"])
    lab.save("evidence/mixed-tesseract.md", body)
    lab.check("mixed pdf original_hash is original", row["original_hash"] == sha256(S["content"].b["mixed.pdf"]), row["original_hash"])
    lab.check("native page text present", MIXED_NATIVE_LINES[0][:40] in body, body[:800])
    upper = body.upper()
    found = [l for l in SCAN_LINES[1:] if l in upper]
    lab.check("scanned page OCR text present (>=2 of 3 lines)", len(found) >= 2, {"found": found, "md": body[:1500]})
    lab.check("native page precedes scanned page", body.find(MIXED_NATIVE_LINES[0][:40]) < upper.find("CONFIDENTIAL"), body[:800])
    document = doc_json(lab, row["result_ref"])
    lab.save("evidence/mixed-tesseract-document.json", document)
    blob = json.dumps(document, ensure_ascii=False)
    lab.check("document records quality_check report", "quality" in blob, blob[:500])
    lab.check("document records tesseract OCR engine", "tesseract" in blob, blob[:500])


def test_scan_no_text_document_fails(lab):
    """Whole-document scan with no OCR text at all: explicit failure, no version."""
    fx = S["fx_scan"]
    url = fx.base + "/scan/noise.pdf"
    rows = rows_for(lab, "dd-040", url)
    evidence = failure_evidence(lab, "dd-040", url)
    lab.save("evidence/noise-failure.json", evidence)
    lab.check("no delivery row for text-less scan", not rows, rows)
    versions = lab.q("""SELECT v.id FROM seal.version v JOIN seal.resource r ON r.id=v.resource_id WHERE r.external_key=%s""", (url,))
    lab.check("no version for text-less scan", not versions, versions)
    lab.check("explicit failed observation with error code", any(o["outcome"] == "failed" and o["error"] for o in evidence["observations"]), evidence)
    lab.check("receipt counts the failed document", counts(S["receipt_scan"]).get("failed", 0) >= 1, counts(S["receipt_scan"]))
    lab.check("original still archived as evidence", any(a["state"] == "archived" for a in evidence["acquisitions"]), evidence["acquisitions"])


def test_scan_quality_gate_blocks_page_without_ocr_text(lab):
    """Native page OK but scanned page has ~no OCR text: quality_check fails, no version."""
    fx = S["fx_scan"]
    url = fx.base + "/scan/mixed-noise.pdf"
    rows = rows_for(lab, "dd-040", url)
    evidence = failure_evidence(lab, "dd-040", url)
    lab.save("evidence/mixed-noise-failure.json", evidence)
    if rows:
        lab.save("evidence/mixed-noise-accepted.md", md(lab, rows[0]["markdown_hash"]))
        lab.save("evidence/mixed-noise-accepted-document.json", doc_json(lab, rows[0]["result_ref"]))
    lab.check("no delivery row for document with an unreadable scanned page", not rows,
              [{k: r[k] for k in ("title", "markdown_hash")} for r in rows])
    versions = lab.q("""SELECT v.id FROM seal.version v JOIN seal.resource r ON r.id=v.resource_id WHERE r.external_key=%s""", (url,))
    lab.check("no version", not versions, versions)
    lab.check("failure reason is quality_check_failed:<route>", "quality_check_failed" in json.dumps(evidence, default=str), evidence)


def test_collect_scan_br_ocr(lab):
    """Mixed scanned PDF through controllable br-ocr-v1 service."""
    mock = BrOcrMock()
    S["br"] = mock
    mock.result_factory = lambda req, raw: br_mixed_result("BRMOCK-7781")
    fx = Fixture()
    S["fx_br"] = fx
    fx.put("/br/mixed.pdf", S["content"].b["mixed.pdf"], "application/pdf")
    cfg = base_source("dd-041", fx, ["/br/mixed.pdf"], ocr={"engine": "br-ocr-v1", "model": "mock-layout-v1", "timeout_seconds": 60, "poll_seconds": 0.2})
    result, receipt, _ = collect(lab, "dd-041", cfg, label="collect br-ocr", env_extra={"SEAL_BR_OCR_URL": mock.base})
    S["br_cfg"] = cfg
    lab.save("receipts/dd-041-br-ocr.json", receipt or result.stdout)
    lab.save("evidence/br-ocr-calls.json", mock.calls)
    lab.check("collect br-ocr exit 0", result.returncode == 0, result.stderr[-1500:])
    lab.check("br-ocr /file_parse called", any(c["method"] == "POST" and c["path"].endswith("/file_parse") for c in mock.calls), mock.calls)
    lab.check("br-ocr /status and /result polled", any("/status/" in c["path"] for c in mock.calls) and any("/result/" in c["path"] for c in mock.calls), mock.calls)
    row = single_row(lab, "dd-041", fx.base + "/br/mixed.pdf")
    body = md(lab, row["markdown_hash"])
    lab.save("evidence/mixed-br-ocr.md", body)
    lab.check("br-ocr scanned page text used (marker)", "BRMOCK-7781" in body, body[:1500])
    lab.check("br-ocr table content kept", "Five years" in body, body[:1500])
    lab.check("native page text present", MIXED_NATIVE_LINES[0][:40] in body, body[:800])
    blob = json.dumps(doc_json(lab, row["result_ref"]), ensure_ascii=False)
    lab.check("document records br-ocr-v1 engine", "br-ocr-v1" in blob, blob[:500])
    S["br_v1"] = lab.q("SELECT current_version_id FROM seal.resource WHERE external_key=%s", (fx.base + "/br/mixed.pdf",))[0]["current_version_id"]


