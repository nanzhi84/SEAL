"""Independent Issue 31 business scenarios; product access uses CLI, SQL, S3 and HTTP."""
# ruff: noqa: F403,F405
from issue31_common import *

# ============================================================ row structure / identity (dd-033)


def setup_structure(lab):
    c = S["content"]
    fx = Fixture()
    S["fx_struct"] = fx
    details = {
        "/detail/single": structure_detail("Single Notice Page"),
        # volume links deliberately in reverse page order
        "/detail/volumes": structure_detail("Consolidated Code", [("/files/vol2.pdf", "Phần 2"), ("/files/vol1.pdf", "Phần 1")]),
        "/detail/attach": structure_detail("Decree on Data Management", [("/files/main.docx", "Main decision"), ("/files/appendix.xlsx", "Appendix table")], FIELD_BLOCK),
        "/detail/amb-dup": structure_detail("Ambiguous Duplicate", [("/files/amb1.pdf", "Phần 1"), ("/files/amb2.pdf", "Phần 1")]),
        "/detail/amb-gap": structure_detail("Ambiguous Gap", [("/files/amb1.pdf", "Phần 1"), ("/files/gap3.pdf", "Phần 3")]),
        "/detail/missing": structure_detail("Missing Annex", [("/files/missing.pdf", "Annex document")]),
        "/detail/reset": structure_detail("Reset Annex", [("/files/reset.pdf", "Annex document")]),
        "/detail/samename-a": structure_detail("Annual Inspection Report", [("/a/report.pdf", "report.pdf")]),
        "/detail/samename-b": structure_detail("Annual Inspection Report", [("/b/report.pdf", "report.pdf")]),
    }
    for path, page in details.items():
        fx.put(path, page)
    fx.put("/list", html_page("Structure listing", "<p>Navigation only</p>", links=[(p, p) for p in details]))
    for name, path in [("vol1.pdf", "/files/vol1.pdf"), ("vol2.pdf", "/files/vol2.pdf"), ("amb1.pdf", "/files/amb1.pdf"),
                       ("amb2.pdf", "/files/amb2.pdf"), ("gap3.pdf", "/files/gap3.pdf"), ("report-a.pdf", "/a/report.pdf"),
                       ("report-b.pdf", "/b/report.pdf")]:
        fx.put(path, c.b[name], "application/pdf")
    fx.put("/files/main.docx", c.b["main.docx"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    fx.put("/files/appendix.xlsx", c.b["appendix.xlsx"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    fx.routes["/files/reset.pdf"] = ("reset", "application/pdf", c.b["reset.pdf"], {})
    return fx


def test_collect_structure(lab):
    """Detail/attachment adapter collect (volumes, attachments, failures)."""
    fx = setup_structure(lab)
    cfg = base_source("dd-033", fx, ["/list"], documents={"detail_url_patterns": [r".*/detail/.*"], "fields": FIELD_RULES},
                      metadata={"source_name": "结构验收来源", "document_type": "法律法规", "language": "en"})
    result, receipt, _ = collect(lab, "dd-033", cfg, label="collect structure")
    S["receipt_struct"] = receipt
    lab.save("receipts/dd-033-structure.json", receipt or result.stdout)
    lab.check("collect structure returns receipt", isinstance(receipt, dict), result.stdout[-2000:] + result.stderr[-2000:])
    lab.check("receipt reports the expected failed documents (>=4: dup, gap, 404, reset)", counts(receipt).get("failed", 0) >= 4, counts(receipt))


def test_rows_single_file(lab):
    """Single file document: one row, 文件顺序 empty, HTML original."""
    fx = S["fx_struct"]
    row = single_row(lab, "dd-033", fx.base + "/detail/single")
    lab.check("single 文件顺序 empty", row["volume_order"] is None, row)
    lab.check("single 来源详情页 is detail URL", row["detail_url"] == fx.base + "/detail/single", row["detail_url"])
    lab.check("single original is the detail HTML", row["original_hash"] == sha256(fx.routes["/detail/single"][2]), row["original_hash"])
    lab.check("single 资料名称", row["title"] == "Single Notice Page", row["title"])
    lab.check("single file names", row["markdown_file"] == f"Single Notice Page_{row['markdown_hash'][:8]}.md"
              and row["original_file"] == f"Single Notice Page_{row['original_hash'][:8]}.html", [row["markdown_file"], row["original_file"]])
    lab.check("来源记录号 is 64 lowercase hex", re.fullmatch(r"[0-9a-f]{64}", row["source_record_hash"] or "") is not None, row["source_record_hash"])


def test_rows_consecutive_volumes(lab):
    """Two volumes: rows 1,2 by label (not page order), shared name/record, _01/_02 names."""
    fx, c = S["fx_struct"], S["content"]
    rows = rows_for(lab, "dd-033", fx.base + "/detail/volumes")
    lab.check("two volume rows", len(rows) == 2, {"rows": rows, "fail": failure_evidence(lab, "dd-033", fx.base + "/detail/volumes") if len(rows) != 2 else None})
    by_order = {r["volume_order"]: r for r in rows}
    lab.check("文件顺序 = 1, 2", sorted(by_order) == [1, 2], list(by_order))
    lab.check("volume 1 is Phần 1 file (label order, not page order)", by_order[1]["original_hash"] == sha256(c.b["vol1.pdf"]), by_order[1]["original_hash"])
    lab.check("volume 2 is Phần 2 file", by_order[2]["original_hash"] == sha256(c.b["vol2.pdf"]), by_order[2]["original_hash"])
    lab.check("volumes share 资料名称", by_order[1]["title"] == by_order[2]["title"] == "Consolidated Code", [r["title"] for r in rows])
    lab.check("volumes share 来源记录号", by_order[1]["source_record_hash"] == by_order[2]["source_record_hash"], [r["source_record_hash"] for r in rows])
    for n in (1, 2):
        r = by_order[n]
        lab.check(f"volume {n} 解析后文件 <资料名称>_<hash8>_0{n}.md", r["markdown_file"] == f"Consolidated Code_{r['markdown_hash'][:8]}_0{n}.md", r["markdown_file"])
        lab.check(f"volume {n} 原始文件 <资料名称>_<hash8>_0{n}.pdf", r["original_file"] == f"Consolidated Code_{r['original_hash'][:8]}_0{n}.pdf", r["original_file"])
        text = "VOLUME ONE CONTENT" if n == 1 else "VOLUME TWO CONTENT"
        lab.check(f"volume {n} markdown is its own file", text in md(lab, r["markdown_hash"]), r["markdown_hash"])


def test_rows_independent_attachments(lab):
    """Main + independent attachment: own names, shared record, empty 文件顺序, fields mapped."""
    fx, c = S["fx_struct"], S["content"]
    rows = rows_for(lab, "dd-033", fx.base + "/detail/attach")
    lab.check("two attachment rows", len(rows) == 2, {"rows": rows, "fail": failure_evidence(lab, "dd-033", fx.base + "/detail/attach") if len(rows) != 2 else None})
    by_hash = {r["original_hash"]: r for r in rows}
    main, appendix = by_hash.get(sha256(c.b["main.docx"])), by_hash.get(sha256(c.b["appendix.xlsx"]))
    lab.check("main and appendix originals both delivered", main is not None and appendix is not None, list(by_hash))
    lab.check("main row 资料名称 = document title", main["title"] == "Decree on Data Management", main["title"])
    lab.check("attachment row 资料名称 = link text", appendix["title"] == "Appendix table", appendix["title"])
    lab.check("文件顺序 empty on both", main["volume_order"] is None and appendix["volume_order"] is None, [main["volume_order"], appendix["volume_order"]])
    lab.check("shared 来源记录号", main["source_record_hash"] == appendix["source_record_hash"], [main["source_record_hash"], appendix["source_record_hash"]])
    lab.check("shared 来源详情页", main["detail_url"] == appendix["detail_url"] == fx.base + "/detail/attach", [main["detail_url"], appendix["detail_url"]])
    lab.check("attachment file names", appendix["markdown_file"] == f"Appendix table_{appendix['markdown_hash'][:8]}.md"
              and appendix["original_file"] == f"Appendix table_{appendix['original_hash'][:8]}.xlsx", [appendix["markdown_file"], appendix["original_file"]])
    lab.check("main markdown is docx text", "Main decision text adopted by the council." in md(lab, main["markdown_hash"]), main["markdown_hash"])
    lab.check("appendix markdown is xlsx table", "appendix row content" in md(lab, appendix["markdown_hash"]), appendix["markdown_hash"])
    expected = {"document_number": "12/2024/NĐ-CP", "issuer": "Chính phủ", "published_at": "2024-03-15", "effective_at": "2024-05-01",
                "status": "Còn hiệu lực", "expired_at": None}
    got = {k: (str(main[k]) if main[k] is not None else None) for k in expected}
    lab.check("declarative fields mapped (文号/发布机构/发布日期/生效日期/状态; 失效日期 empty)", got == expected, {"got": got, "expected": expected})


def _blocked(lab, path, reason_hint):
    fx = S["fx_struct"]
    url = fx.base + path
    rows = rows_for(lab, "dd-033", url)
    versions = lab.q("SELECT v.id FROM seal.version v JOIN seal.resource r ON r.id=v.resource_id WHERE r.external_key=%s", (url,))
    evidence = failure_evidence(lab, "dd-033", url)
    lab.save(f"evidence/failure-{path.strip('/').replace('/', '-')}.json", evidence)
    lab.check(f"{path}: no delivery row", not rows, rows)
    lab.check(f"{path}: no version", not versions, versions)
    text = json.dumps(evidence, default=str, ensure_ascii=False)
    lab.check(f"{path}: explicit failure recorded ({reason_hint})", ("failed" in text) and (reason_hint is None or reason_hint in text), evidence)


def test_rows_ambiguous_volume_duplicate_blocked(lab):
    """Duplicate volume numbers block assembly (volume_order_ambiguous)."""
    _blocked(lab, "/detail/amb-dup", "volume_order_ambiguous")


def test_rows_ambiguous_volume_gap_blocked(lab):
    """Non-consecutive volume numbers block assembly (volume_order_ambiguous)."""
    _blocked(lab, "/detail/amb-gap", "volume_order_ambiguous")


def test_failure_attachment_404(lab):
    """Required attachment returns 404: document fails, no partial success."""
    _blocked(lab, "/detail/missing", None)
    acq = lab.q("SELECT state, http_status, outcome FROM seal.acquisition WHERE request_url=%s", (S["fx_struct"].base + "/files/missing.pdf",))
    lab.check("404 attachment acquisition recorded with HTTP status", any(a["http_status"] == 404 for a in acq), acq)


def test_failure_attachment_connection_reset(lab):
    """Attachment connection dropped mid-body: failure, no archive_ref for partial bytes."""
    _blocked(lab, "/detail/reset", None)
    acq = lab.q("SELECT state, body_hash, archive_ref, error, outcome FROM seal.acquisition WHERE request_url=%s", (S["fx_struct"].base + "/files/reset.pdf",))
    lab.check("reset acquisition is failed, not archived", acq and all(a["state"] != "archived" for a in acq), acq)
    lab.check("no archive_ref for incomplete bytes", all(a["archive_ref"] is None for a in acq), acq)


def test_identity_same_named_originals(lab):
    """Same title + same original file name in two documents never merge."""
    fx, c = S["fx_struct"], S["content"]
    a = rows_for(lab, "dd-033", fx.base + "/detail/samename-a")
    b = rows_for(lab, "dd-033", fx.base + "/detail/samename-b")
    lab.check("each document has exactly one row", len(a) == 1 and len(b) == 1, {"a": a, "b": b})
    a, b = a[0], b[0]
    lab.check("distinct resources", a["rid"] != b["rid"], [a["rid"], b["rid"]])
    lab.check("distinct 来源记录号", a["source_record_hash"] != b["source_record_hash"], [a["source_record_hash"], b["source_record_hash"]])
    lab.check("each row keeps its own original", a["original_hash"] == sha256(c.b["report-a.pdf"]) and b["original_hash"] == sha256(c.b["report-b.pdf"]),
              [a["original_hash"], b["original_hash"]])
    lab.check("distinct 解析后文件 names", a["markdown_file"] != b["markdown_file"], [a["markdown_file"], b["markdown_file"]])


def test_listing_page_not_deliverable(lab):
    """Navigation listing page is archived but is not a document."""
    fx = S["fx_struct"]
    url = fx.base + "/list"
    acq = lab.q("SELECT body_hash, state FROM seal.acquisition WHERE request_url=%s AND state='archived'", (url,))
    lab.check("listing archived in CAS", bool(acq), acq)
    S["listing_hash"] = acq[0]["body_hash"]
    lab.check("listing has no delivery row", not rows_for(lab, "dd-033", url), rows_for(lab, "dd-033", url))


def test_identity_same_number_different_sources(lab):
    """Same official number (and same URL) in two sources: separate resources/records."""
    fx = Fixture()
    S["fx_same"] = fx
    fx.put("/same/list", html_page("Same list", links=[("/same/detail", "detail")]))
    fx.put("/same/detail", structure_detail("Shared Number Decree", extra=FIELD_BLOCK))
    out = {}
    for sid in ("dd-051", "dd-061"):
        cfg = base_source(sid, fx, ["/same/list"], documents={"detail_url_patterns": [r".*/same/detail$"], "fields": FIELD_RULES})
        result, receipt, _ = collect(lab, sid, cfg, label=f"collect same-number {sid}")
        lab.check(f"collect {sid} exit 0", result.returncode == 0, result.stderr[-1000:])
        rows = rows_for(lab, sid, fx.base + "/same/detail")
        lab.check(f"{sid} one row", len(rows) == 1, rows)
        out[sid] = rows[0]
    a, b = out["dd-051"], out["dd-061"]
    lab.check("same 文号 in both", a["document_number"] == b["document_number"] == "12/2024/NĐ-CP", [a["document_number"], b["document_number"]])
    lab.check("different resources", a["rid"] != b["rid"], [a["rid"], b["rid"]])
    lab.check("different 来源记录号", a["source_record_hash"] != b["source_record_hash"], [a["source_record_hash"], b["source_record_hash"]])


