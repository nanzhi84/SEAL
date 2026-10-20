"""Independent Issue 31 business scenarios; product access uses CLI, SQL, S3 and HTTP."""
# ruff: noqa: F403,F405
from issue31_common import *

# ============================================================ TCVN3 (dd-176)


def test_collect_tcvn3(lab):
    """Vietnamese TCVN3 RTF (and DOC via LibreOffice) collect."""
    c = S["content"]
    fx = Fixture()
    S["fx_tcvn3"] = fx
    paths = []
    fx.put("/vn/luat-tcvn3.rtf", c.b["tcvn3.rtf"], "application/rtf")
    paths.append("/vn/luat-tcvn3.rtf")
    if "tcvn3.doc" in c.b:
        fx.put("/vn/luat-tcvn3.doc", c.b["tcvn3.doc"], "application/msword")
        paths.append("/vn/luat-tcvn3.doc")
    cfg = base_source("dd-176", fx, paths, metadata={"source_name": "Cổng thông tin thử nghiệm", "document_type": "法律法规", "language": "vi"},
                      ocr={"engine": "tesseract", "languages": "vie", "dpi": 150})
    result, receipt, _ = collect(lab, "dd-176", cfg, label="collect tcvn3")
    lab.save("receipts/dd-176-tcvn3.json", receipt or result.stdout)
    lab.check("collect tcvn3 exit 0", result.returncode == 0, result.stderr[-1500:])
    lab.check("no failed TCVN3 documents", counts(receipt).get("failed") == 0,
              {"counts": counts(receipt), "fail": [failure_evidence(lab, "dd-176", fx.base + p) for p in paths]})


def _tcvn3_checks(lab, path, label, data):
    from issue31_fixtures import TCVN3_UNICODE_HEAD

    row = single_row(lab, "dd-176", S["fx_tcvn3"].base + path)
    body = md(lab, row["markdown_hash"])
    lab.save(f"evidence/{label}.md", body)
    S.setdefault("tcvn3_bodies", {})[label] = body
    with lab.soft():
        _tcvn3_assertions(lab, label, row, body, data, TCVN3_UNICODE_HEAD)
    return body


def _tcvn3_assertions(lab, label, row, body, data, TCVN3_UNICODE_HEAD):
    lab.check(f"{label} original_hash is served original", row["original_hash"] == sha256(data), row["original_hash"])
    safe_head = [h for h in TCVN3_UNICODE_HEAD if "Ò" not in h]
    missing_head = [h for h in safe_head if h not in body]
    lab.check(f"{label} already-Unicode header/date lines preserved", not missing_head, {"missing": missing_head, "md": body[:800]})
    for text in ("Luật này quy định về việc thành lập, tổ chức, hoạt động của tổ chức tín dụng.",
                 "Luật này áp dụng đối với các tổ chức tín dụng và cơ quan, tổ chức, cá nhân có liên quan."):
        lab.check(f"{label} TCVN3 body recovered to Unicode", text in body, {"expected": text, "md": body[:1500]})
    garbled = [ch for ch in "§¸µ¶·¹¨»¾¼½Æ©ÇÊÈË®ÌÐÎÑªÕÓÔÖ×ÝØÜÞß«åæ¬ëîïñö÷ûüþ¡¢£¤¥¦\u00ad" if ch in body]
    lab.check(f"{label} no TCVN3 glyph garbage left", not garbled, garbled)
    patterns = {"Phần → #": r"^#\s+Phần thứ nhất", "Chương → #": r"^#\s+Chương I\b", "Mục → ##": r"^##\s+Mục 1\b",
                "Điều → ###": r"^###\s+Điều 1\.", "Điều 2 → ###": r"^###\s+Điều 2\."}
    bad = {k: p for k, p in patterns.items() if not re.search(p, body, re.M)}
    lab.check(f"{label} Vietnamese legal heading levels restored", not bad, {"missing": bad, "md": body[:1500]})
    lab.check(f"{label} RTF header/footer noise removed", "NOISE-SHOULD-NOT-APPEAR" not in body, body[:400])
    # The reference LEGAL_TITLE rule permits this intact Luật line as a title.
    protected_head = [h for h in safe_head if h != "Luật số: 32/2024/QH15"]
    promoted = [h for h in protected_head if re.search(r"^#+\s+" + re.escape(h), body, re.M)]
    lab.check(f"{label} Unicode agency/date lines not rewritten into Markdown headings", not promoted, promoted)


def test_tcvn3_rtf_mixed_encoding(lab):
    """TCVN3 RTF: line-level recovery, Unicode header intact, headings."""
    _tcvn3_checks(lab, "/vn/luat-tcvn3.rtf", "tcvn3-rtf", S["content"].b["tcvn3.rtf"])


def test_tcvn3_doc_mixed_encoding(lab):
    """TCVN3 DOC (LibreOffice): same recovery guarantees."""
    if "tcvn3.doc" not in S["content"].b:
        raise NotVerified("LibreOffice could not generate the .doc fixture")
    _tcvn3_checks(lab, "/vn/luat-tcvn3.doc", "tcvn3-doc", S["content"].b["tcvn3.doc"])


def test_tcvn3_unicode_header_with_overlapping_glyph(lab):
    """Stricter: Unicode 'CỘNG HÒA' header (Ò overlaps a TCVN3 glyph) not corrupted."""
    bodies = S.get("tcvn3_bodies") or {}
    if not bodies:
        raise AssertionError("no TCVN3 markdown available")
    bad = {k: v[:300] for k, v in bodies.items() if "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM" not in v}
    lab.check("CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM preserved byte-exact", not bad, bad)


