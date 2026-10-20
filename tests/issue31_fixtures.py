"""Issue #31 controllable source content (bytes are generated here, never fetched)."""

from __future__ import annotations

from issue31_support import (
    BrOcrMock, Fixture, PdfBuilder, br_result, docx_bytes, html_page, noise_page_jpeg, plain_rtf,
    scanned_page_jpeg, soffice_convert, tcvn3_rtf, xlsx_bytes, zip_bytes,
)

# ---------------------------------------------------------------- texts

NATIVE_PDF_PAGES = [
    ["NATIVE PDF PAGE ONE: Regulation on public disclosure of financial reports.",
     "Article 1. Listed companies shall publish audited annual statements."],
    ["NATIVE PDF PAGE TWO: Article 2. Interim reports are due within sixty days.",
     "Article 3. Supervisory authority may request clarifications at any time."],
    ["NATIVE PDF PAGE THREE: Article 4. This regulation enters into force on signing."],
]

MIXED_NATIVE_LINES = [
    "MIXED DOCUMENT PAGE ONE NATIVE TEXT: Agreement on cross border cooperation.",
    "The parties agree to exchange supervisory information in good faith.",
]
SCAN_LINES = ["SCANNED PAGE TWO", "CONFIDENTIAL AGREEMENT", "BRAVO DELTA SIGNATURE", "WITNESS STATEMENT"]
SIGNATURE_NATIVE = "Digitally signed"  # < 30 native chars on the scanned page

TCVN3_UNICODE_HEAD = [
    "QUỐC HỘI",
    "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM",
    "Độc lập - Tự do - Hạnh phúc",
    "Luật số: 32/2024/QH15",
    "Hà Nội, ngày 18 tháng 1 năm 2024",
]
TCVN3_BODY = [
    "Phần thứ nhất",
    "Những quy định chung",
    "Chương I",
    "Phạm vi điều chỉnh và đối tượng áp dụng",
    "Mục 1",
    "Quy định chung về tổ chức tín dụng",
    "Điều 1. Phạm vi điều chỉnh",
    "Luật này quy định về việc thành lập, tổ chức, hoạt động của tổ chức tín dụng.",
    "Điều 2. Đối tượng áp dụng",
    "Luật này áp dụng đối với các tổ chức tín dụng và cơ quan, tổ chức, cá nhân có liên quan.",
]
TCVN3_TAIL = ["CHỦ TỊCH QUỐC HỘI", "(Đã ký)", "Vương Đình Huệ"]

DOCX_TABLE = [["Merged header", "Merged header", "Third"], ["alpha cell", "beta cell", "gamma cell"], ["delta", "epsilon", "zeta"]]
SHEET_ROWS = [["Code", "Name", "Amount"], ["A-01", "Spreadsheet row one", 1200], ["A-02", "Spreadsheet row two", 3400]]
DISGUISED_ROWS = [["Ticker", "Disguised workbook value"], ["DSG-1", "hidden spreadsheet payload"], ["DSG-2", "second disguised row"]]


def pdf_native(pages):
    builder = PdfBuilder()
    for lines in pages:
        builder.page(lines)
    return builder.build()


def pdf_mixed():
    jpeg, size = scanned_page_jpeg(SCAN_LINES)
    return PdfBuilder().page(MIXED_NATIVE_LINES).page(image_jpeg=jpeg, image_size=size, small_text=SIGNATURE_NATIVE).build()


def pdf_noise():
    jpeg, size = noise_page_jpeg()
    return PdfBuilder().page(image_jpeg=jpeg, image_size=size).build()


MIXED_NOISE_NATIVE = ["QUALITY GATE DOCUMENT PAGE ONE: native text page with plenty of characters.",
                      "The second page is a dark scan without any recognisable words."]


def pdf_mixed_noise():
    """Good native page + scanned page with (almost) no OCR text."""
    jpeg, size = noise_page_jpeg()
    return PdfBuilder().page(MIXED_NOISE_NATIVE).page(image_jpeg=jpeg, image_size=size, small_text="Signed").build()


def br_mixed_result(marker):
    return br_result([
        [{"type": "text", "text": line} for line in MIXED_NATIVE_LINES],
        [{"type": "header", "text": "SCANNED PAGE TWO"},
         {"type": "text", "text": f"CONFIDENTIAL AGREEMENT {marker}"},
         {"type": "text", "text": "BRAVO DELTA SIGNATURE WITNESS STATEMENT"},
         {"type": "table", "table_body": "<table><tr><td>Clause</td><td>Value</td></tr><tr><td>Term</td><td>Five years</td></tr></table>"}],
    ])


class Content:
    """All fixture bytes, keyed by logical name."""

    def __init__(self, workdir):
        self.workdir = workdir
        self.b = {}
        b = self.b
        b["page.html"] = html_page(
            "HTML Format Document",
            "<p>The HTML format document body explains acceptance of rendered web content.</p>"
            "<h2>Section heading</h2><p>Second paragraph with <strong>bold</strong> words and more text.</p>",
        ).encode()
        b["note.txt"] = ("TXT format document first line.\nSecond line keeps its own break.\n\n"
                         "Paragraph two of the plain text note * with markdown metachar.\n").encode()
        b["report.docx"] = docx_bytes(
            "DOCX Heading Title", ["DOCX first paragraph explains the report scope.", "DOCX second paragraph lists obligations."],
            table=DOCX_TABLE, headings=[(2, "DOCX Subheading Two")],
        )
        b["plain.rtf"] = plain_rtf(["RTF plain document first paragraph about governance.",
                                    "RTF second paragraph about compliance reporting duties."])
        b["table.xlsx"] = xlsx_bytes(SHEET_ROWS)
        b["disguised.docx"] = xlsx_bytes(DISGUISED_ROWS, sheet="Hidden")  # xlsx bytes named .docx
        inner_docx = docx_bytes("Zipped Inner Report", ["Inner docx paragraph inside the zip bundle."])
        b["bundle.zip"] = zip_bytes({"bundle/inner-report.docx": inner_docx,
                                     "bundle/inner-notes.txt": "Inner text file content from the zip bundle.\n".encode()})
        b["native.pdf"] = pdf_native(NATIVE_PDF_PAGES)
        b["mixed.pdf"] = pdf_mixed()
        b["noise.pdf"] = pdf_noise()
        b["mixed-noise.pdf"] = pdf_mixed_noise()
        b["tcvn3.rtf"] = tcvn3_rtf(TCVN3_UNICODE_HEAD, TCVN3_BODY, TCVN3_TAIL)
        # LibreOffice-generated legacy binaries (fixture generation only)
        self.lo_error = None
        try:
            b["report.doc"] = soffice_convert(
                docx_bytes("DOC Legacy Title", ["DOC legacy paragraph converted by LibreOffice.", "DOC second legacy paragraph."]),
                "docx", "doc:MS Word 97", workdir / "lo")
            b["table.xls"] = soffice_convert(xlsx_bytes([["Key", "Legacy xls value"], ["X-1", "xls row content one"]]),
                                             "xlsx", "xls:MS Excel 97", workdir / "lo")
            b["disguised.doc"] = soffice_convert(xlsx_bytes([["Key", "Disguised xls value"], ["Q-9", "xls named doc payload"]]),
                                                 "xlsx", "xls:MS Excel 97", workdir / "lo")
            b["tcvn3.doc"] = soffice_convert(b["tcvn3.rtf"], "rtf", "doc:MS Word 97", workdir / "lo")
        except Exception as exc:  # noqa: BLE001
            self.lo_error = f"{type(exc).__name__}: {exc}"
        # structure source attachments
        b["vol1.pdf"] = pdf_native([["VOLUME ONE CONTENT: Part one of the consolidated code, articles 1 to 50."]])
        b["vol2.pdf"] = pdf_native([["VOLUME TWO CONTENT: Part two of the consolidated code, articles 51 to 99."]])
        b["main.docx"] = docx_bytes("Main Decision Body", ["Main decision text adopted by the council."])
        b["appendix.xlsx"] = xlsx_bytes([["Item", "Appendix value"], ["AP-1", "appendix row content"]])
        b["amb1.pdf"] = pdf_native([["AMBIGUOUS VOLUME FIRST FILE with enough native characters."]])
        b["amb2.pdf"] = pdf_native([["AMBIGUOUS VOLUME SECOND FILE with enough native characters."]])
        b["gap3.pdf"] = pdf_native([["GAP VOLUME THREE FILE with enough native characters here."]])
        b["report-a.pdf"] = pdf_native([["SAME NAME REPORT A: annual inspection findings for district alpha."]])
        b["report-b.pdf"] = pdf_native([["SAME NAME REPORT B: annual inspection findings for district bravo."]])
        b["reset.pdf"] = pdf_native([["RESET TARGET never delivered completely to the collector."]])


def structure_detail(title, links=(), extra=""):
    return html_page(title, f"<p>Detail page for {title}. Public notice body text for acceptance.</p>{extra}", links=links)


FIELD_BLOCK = (
    "<div class='meta'><span class='num'>Số: 12/2024/NĐ-CP</span>"
    "<span class='issuer'>Chính phủ</span><span class='date'>15/03/2024</span>"
    "<span class='eff'>01/05/2024</span><span class='status'>Còn hiệu lực</span></div>"
)

FIELD_RULES = {
    "document_number": {"xpath": "//span[@class='num']", "regex": r"(?P<value>\d+/\d{4}/[A-ZĐ-]+)"},
    "issuer": {"xpath": "//span[@class='issuer']"},
    "published_at": {"xpath": "//span[@class='date']", "date_format": "%d/%m/%Y"},
    "effective_at": {"xpath": "//span[@class='eff']", "date_format": "%d/%m/%Y"},
    "status": {"xpath": "//span[@class='status']"},
}
