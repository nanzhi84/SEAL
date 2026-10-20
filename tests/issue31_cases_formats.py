"""Independent Issue 31 business scenarios; product access uses CLI, SQL, S3 and HTTP."""
# ruff: noqa: F403,F405
from issue31_common import *

# ============================================================ setup / init


def test_init_new_contract_schema(lab):
    """seal init creates the new contract schema on an empty database."""
    result = lab.cli("init", label="init")
    lab.check("seal init exits 0", result.returncode == 0, result.stderr[-2000:])
    tables = {r["table_name"] for r in lab.q("SELECT table_name FROM information_schema.tables WHERE table_schema='seal'")}
    needed = {"acquisition", "task", "task_input", "resource", "observation", "observation_evidence", "delivery_conflict",
              "version", "delivery_row", "snapshot", "snapshot_item", "snapshot_file", "snapshot_source", "publication"}
    lab.check("contract tables exist", needed <= tables, sorted(needed - tables))
    cols = {r["column_name"]: r["is_nullable"] for r in lab.q(
        "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_schema='seal' AND table_name='resource'")}
    lab.check("resource identity frontier_id/identity_namespace/external_key NOT NULL",
              all(cols.get(c) == "NO" for c in ("frontier_id", "identity_namespace", "external_key")), cols)
    lab.check("resource.public_url nullable (documents without public_url allowed)", cols.get("public_url") == "YES", cols)
    lab.check("resource has independent recheck_locator", "recheck_locator" in cols, cols)


# ============================================================ formats (dd-017)


def setup_format_fixture(lab):
    c = S["content"]
    fx = Fixture()
    S["fx_formats"] = fx
    urls = []
    for name, media in FORMAT_FILES.items():
        if name not in c.b:
            continue
        fx.put("/f/" + name, c.b[name], media)
        urls.append("/f/" + name)
    return fx, urls


def test_collect_formats_chain(lab):
    """HTML/TXT/Office/ZIP/native PDF collect through the real CLI."""
    if S["content"].lo_error:
        S["lo_missing"] = S["content"].lo_error
    fx, urls = setup_format_fixture(lab)
    cfg = base_source("dd-017", fx, urls, metadata={"source_name": "格式验收来源", "document_type": "政策文件", "language": "en"})
    result, receipt, _ = collect(lab, "dd-017", cfg, label="collect formats")
    S["receipt_formats"] = receipt
    lab.save("receipts/dd-017-formats.json", receipt or result.stdout)
    lab.check("collect formats exit 0", result.returncode == 0, {"exit": result.returncode, "stderr": result.stderr[-1500:]})
    lab.check("receipt uses a structured contract", isinstance(receipt, dict) and receipt.get("contract", "").startswith("seal.receipt"),
              (receipt or {}).get("contract"))
    lab.check("no failed documents in format collect", counts(receipt).get("failed") == 0, counts(receipt))
    archived = lab.q("SELECT request_url, state FROM seal.acquisition WHERE source_id='dd-017'")
    lab.check("every format URL archived", all(any(a["request_url"] == fx.base + u and a["state"] == "archived" for a in archived) for u in urls),
              archived)


def _fmt(lab, name):
    fx = S.get("fx_formats")
    if fx is None:
        raise AssertionError("format fixture not collected")
    if name not in S["content"].b:
        raise NotVerified(f"{name} fixture could not be generated: {S['content'].lo_error}")
    return single_row(lab, "dd-017", fx.base + "/f/" + name), S["content"].b[name]


def test_format_html(lab):
    """HTML page becomes a single-file row with Markdown body."""
    row, data = _fmt(lab, "page.html")
    body = check_file_row(lab, row, data, ["The HTML format document body", "Section heading", "bold"], "html", "html")
    lab.check("html 资料名称 from <h1>/<title>", row["title"] == "HTML Format Document", row["title"])
    lab.check("html 资料类型 from source metadata", row["document_type"] == "政策文件", row["document_type"])
    lab.check("html 来源名称 from source metadata", row["source_name"] == "格式验收来源", row["source_name"])
    lab.check("html markdown has no raw tags", "<p>" not in body and "<main>" not in body, body[:300])


def test_format_txt(lab):
    """TXT keeps lines/paragraphs and escapes markdown metacharacters."""
    row, data = _fmt(lab, "note.txt")
    body = check_file_row(lab, row, data, ["TXT format document first line.", "Second line keeps its own break."], "txt", "txt")
    lines = body.splitlines()
    lab.check("txt line break preserved", any("TXT format document first line." in l and "Second line" not in l for l in lines), body)
    lab.check("txt markdown metachar escaped (\\*)", "\\*" in body, body)


def test_format_docx(lab):
    """DOCX structured extraction: headings, paragraphs, merged-cell table."""
    row, data = _fmt(lab, "report.docx")
    body = check_file_row(lab, row, data, ["DOCX Heading Title", "DOCX first paragraph", "DOCX second paragraph", "alpha cell", "gamma cell"],
                          "docx", "docx")
    lab.check("docx heading rendered as markdown heading", re.search(r"^#{1,6}\s+DOCX Heading Title", body, re.M) is not None, body[:800])
    lab.check("docx subheading rendered as markdown heading", re.search(r"^#{1,6}\s+DOCX Subheading Two", body, re.M) is not None, body[:800])
    table_lines = [l for l in body.splitlines() if l.strip().startswith("|")]
    lab.check("docx table rendered as markdown table", any("alpha cell" in l and "beta cell" in l for l in table_lines), table_lines)
    header = [l for l in table_lines if "Merged header" in l]
    lab.check("docx horizontally merged cell de-duplicated", header and header[0].count("Merged header") == 1, header)


def test_format_doc_libreoffice(lab):
    """Legacy .doc extracted through headless LibreOffice."""
    row, data = _fmt(lab, "report.doc")
    check_file_row(lab, row, data, ["DOC legacy paragraph converted by LibreOffice.", "DOC second legacy paragraph."], "doc", "doc")


def test_format_rtf(lab):
    """RTF state machine strips fonttbl/stylesheet/pict/header/footer."""
    row, data = _fmt(lab, "plain.rtf")
    body = check_file_row(lab, row, data, ["RTF plain document first paragraph about governance.",
                                           "RTF second paragraph about compliance reporting duties."], "rtf", "rtf")
    leaked = [t for t in ("Times New Roman", "Normal;", "PICTNOISE", "FixtureGen", "89504e47", "\\rtf") if t in body]
    lab.check("rtf control groups not leaked", not leaked, {"leaked": leaked, "markdown": body[:600]})


def test_format_xlsx(lab):
    """XLSX converted to a Markdown table."""
    row, data = _fmt(lab, "table.xlsx")
    body = check_file_row(lab, row, data, ["Spreadsheet row one", "Spreadsheet row two", "A-01"], "xlsx", "xlsx")
    lab.check("xlsx row rendered as table", any(l.strip().startswith("|") and "Spreadsheet row one" in l and "1200" in l for l in body.splitlines()), body)


def test_format_xls(lab):
    """XLS (BIFF) converted to a Markdown table."""
    row, data = _fmt(lab, "table.xls")
    body = check_file_row(lab, row, data, ["xls row content one", "X-1"], "xls", "xls")
    lab.check("xls row rendered as table", any(l.strip().startswith("|") and "xls row content one" in l for l in body.splitlines()), body)


def test_format_disguised_xlsx_as_docx(lab):
    """xlsx bytes served as .docx: detected as spreadsheet, original named .xlsx."""
    row, data = _fmt(lab, "disguised.docx")
    body = check_file_row(lab, row, data, ["hidden spreadsheet payload", "DSG-1"], None, "disguised.docx")
    lab.check("disguised xlsx rendered as table", any(l.strip().startswith("|") and "hidden spreadsheet payload" in l for l in body.splitlines()), body)
    lab.check("disguised .docx original gets real container extension .xlsx", ext_of(row["original_file"]) == "xlsx", row["original_file"])


def test_format_disguised_xls_as_doc(lab):
    """xls bytes served as .doc: detected as spreadsheet, original named .xls."""
    row, data = _fmt(lab, "disguised.doc")
    body = check_file_row(lab, row, data, ["xls named doc payload", "Q-9"], None, "disguised.doc")
    lab.check("disguised xls rendered as table", any(l.strip().startswith("|") and "xls named doc payload" in l for l in body.splitlines()), body)
    lab.check("disguised .doc original gets real container extension .xls", ext_of(row["original_file"]) == "xls", row["original_file"])


def test_format_zip_with_documents(lab):
    """ZIP unpacked; inner DOCX/TXT text extracted with lineage."""
    row, data = _fmt(lab, "bundle.zip")
    check_file_row(lab, row, data, ["Inner docx paragraph inside the zip bundle.", "Inner text file content from the zip bundle."], "zip", "zip")
    document = json.dumps(doc_json(lab, row["result_ref"]), ensure_ascii=False)
    lab.check("zip inner member paths recorded in document lineage", "inner-report.docx" in document and "inner-notes.txt" in document,
              document[:1500])


def test_format_native_pdf(lab):
    """Native text PDF: all pages, in order."""
    row, data = _fmt(lab, "native.pdf")
    texts = [p[0] for p in NATIVE_PDF_PAGES]
    body = check_file_row(lab, row, data, texts, "pdf", "native.pdf")
    positions = [body.find(t) for t in texts]
    lab.check("native pdf pages in order", positions == sorted(positions), positions)


def test_cas_integrity_formats(lab):
    """Every archived acquisition / delivered file is a verified CAS object."""
    bad = []
    for acq in lab.q("SELECT request_url, body_hash, archive_ref, byte_size FROM seal.acquisition WHERE state='archived'"):
        try:
            data = lab.s3_get(acq["archive_ref"])
        except Exception as exc:  # noqa: BLE001
            bad.append({"url": acq["request_url"], "error": str(exc)})
            continue
        if sha256(data) != acq["body_hash"] or acq["archive_ref"] != acq["body_hash"] or len(data) != acq["byte_size"]:
            bad.append({"url": acq["request_url"], "sha": sha256(data), "body_hash": acq["body_hash"]})
    lab.check("archived acquisitions verified against CAS", not bad, bad[:10])
    bad = []
    for row in lab.q("SELECT dr.markdown_hash, dr.file_hash, dr.original_ref, v.result_ref, v.inputs_ref FROM seal.delivery_row dr JOIN seal.version v ON v.id=dr.version_id"):
        for key in ("markdown_hash", "file_hash", "result_ref", "inputs_ref"):
            digest = row[key]
            try:
                data = lab.s3_get(digest)
                if sha256(data) != digest:
                    bad.append({key: digest, "sha": sha256(data)})
            except Exception as exc:  # noqa: BLE001
                bad.append({key: digest, "error": str(exc)})
    lab.check("delivery/version objects verified against CAS", not bad, bad[:10])
    sample = lab.q("SELECT result_ref, inputs_ref FROM seal.version LIMIT 1")[0]
    document, inputs = doc_json(lab, sample["result_ref"]), doc_json(lab, sample["inputs_ref"])
    lab.check("result_ref is seal.document.v1", document.get("schema") == "seal.document.v1", document.get("schema"))
    lab.check("inputs_ref is seal.inputs.v1", inputs.get("schema") == "seal.inputs.v1", inputs.get("schema"))
    lab.save("evidence/sample-document.json", document)
    lab.save("evidence/sample-inputs.json", inputs)


