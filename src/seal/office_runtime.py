"""Office containers, structured direct extraction and Vietnamese TCVN3 recovery.

Core logic refactored from docs/reference/office_text_extract.py. Inputs are always files
materialized from verified CAS objects; LibreOffice runs headless in an isolated profile,
its own process group and a bounded timeout. Windows COM fallbacks are not supported.
"""

import io
import os
import re
import shutil
import signal
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .core import SealError
from .markdown import compact
from .office_text import (
    extract_rtf_plain_text,
    legal_markdown,
    normalize_detected_tcvn3,
    rtf_uses_tcvn3,
)

OFFICE_CONTRACT = "seal.office.v2"
OFFICE_KINDS = {"doc", "docx", "rtf", "xls", "xlsx"}
ZIP_MEMBER_KINDS = OFFICE_KINDS | {"pdf", "txt", "html"}
MAX_ZIP_MEMBERS = 50
MAX_ZIP_DEPTH = 2


def detect(data, hint=""):
    """Real container type from magic bytes and structure, never from a URL suffix."""
    head = data[:8]
    if data[:5] == b"%PDF-" or b"%PDF-" in data[:1024]:
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError, ValueError):
            return "unknown"
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "zip"
    if head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        if "WordDocument".encode("utf-16-le") in data:
            return "doc"
        if "Workbook".encode("utf-16-le") in data or "Book".encode("utf-16-le") in data:
            return "xls"
        return "unknown"
    stripped = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"{\\rtf"):
        return "rtf"
    lowered = stripped[:512].lower()
    if lowered.startswith((b"<!doctype html", b"<html")) or b"<html" in lowered:
        return "html"
    if lowered.startswith(b"<?xml") or lowered.startswith(b"<"):
        return "xml"
    if lowered.startswith((b"{", b"[")):
        return "json"
    try:
        data[:65536].decode("utf-8")
        return "txt"
    except UnicodeDecodeError:
        return hint if hint in {"txt", "html"} else "unknown"


def soffice():
    configured = os.environ.get("SEAL_SOFFICE")
    for candidate in (
        configured,
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def bounded_run(args, timeout, cwd=None, limit=65536):
    """Own process group; stderr never forwarded (it can contain document text/paths)."""
    process = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "HOME")},
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise SealError("libreoffice_timeout") from None
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return process.returncode, output[:limit]


def libreoffice_identity():
    executable = soffice()
    if not executable:
        return {"status": "unavailable"}
    try:
        code, output = bounded_run([executable, "--headless", "--version"], 60)
        if code != 0:
            raise ValueError
        version = output.decode("utf-8", errors="replace").strip().splitlines()[0][:200]
        return {"status": "available", "version": version}
    except (OSError, ValueError, IndexError, SealError):
        return {"status": "unavailable"}


def libreoffice_convert(path, target_format, extension, timeout, kind):
    executable = soffice()
    if not executable:
        raise SealError("libreoffice_unavailable")
    with tempfile.TemporaryDirectory(prefix="seal-office-") as temp:
        folder = Path(temp)
        source = folder / ("input." + kind)
        shutil.copyfile(path, source)
        code, _ = bounded_run(
            [
                executable,
                "-env:UserInstallation=" + (folder / "profile").as_uri(),
                "--headless",
                "--norestore",
                "--convert-to",
                target_format,
                "--outdir",
                str(folder / "out"),
                str(source),
            ],
            timeout,
        )
        target = folder / "out" / ("input." + extension)
        if code != 0 or not target.is_file():
            raise SealError("libreoffice_conversion_failed")
        return target.read_bytes()


def extract_libreoffice_text(path, timeout, kind):
    raw = libreoffice_convert(path, "txt:Text (encoded):UTF8", "txt", timeout, kind)
    text = compact(raw.decode("utf-8", errors="replace"))
    if len(text) < 8:
        raise SealError("libreoffice_text_unusable")
    return text + "\n"


def extract_libreoffice_pdf_text(path, timeout, kind):
    import pypdfium2 as pdfium

    data = libreoffice_convert(path, "pdf:writer_pdf_Export", "pdf", timeout, kind)
    pages = []
    with pdfium.PdfDocument(data) as document:
        for index in range(len(document)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                pages.append(textpage.get_text_bounded().strip())
                textpage.close()
            finally:
                page.close()
    text = compact("\n\n".join(p for p in pages if p))
    if len(text) < 8:
        raise SealError("libreoffice_text_unusable")
    return text + "\n"


def table_markdown(rows):
    """One Markdown table block; the first row is the header (source geometry unknown)."""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def excel_value(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return compact(str(value))


def extract_excel(path, kind):
    """XLS/XLSX workbooks (including spreadsheets disguised as DOC/DOCX) as Markdown tables."""
    sheets = []
    try:
        if kind == "xls":
            import xlrd

            workbook = xlrd.open_workbook(str(path), formatting_info=False, on_demand=True)
            for sheet in workbook.sheets():
                rows = [
                    [excel_value(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
                    for r in range(sheet.nrows)
                ]
                sheets.append((sheet.name, rows))
            extractor = "xlrd"
        else:
            from openpyxl import load_workbook

            with open(path, "rb") as handle:
                workbook = load_workbook(io.BytesIO(handle.read()), read_only=True, data_only=True)
            for sheet in workbook.worksheets:
                rows = [[excel_value(v) for v in row] for row in sheet.iter_rows(values_only=True)]
                sheets.append((sheet.title, rows))
            workbook.close()
            extractor = "openpyxl"
    except Exception:
        raise SealError("spreadsheet_invalid") from None
    parts = []
    for name, rows in sheets:
        nonempty = [row for row in rows if any(cell for cell in row)]
        if not nonempty:
            continue
        width = max(len(row) for row in nonempty)
        rows = [row + [""] * (width - len(row)) for row in nonempty]
        parts.append("# " + compact(str(name)))
        parts.append(table_markdown([[c.replace("|", "\\|") for c in row] for row in rows]))
    value = "\n\n".join(parts).strip()
    if len(value) < 8:
        raise SealError("spreadsheet_empty")
    return value + "\n", extractor


def extract_docx(path):
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = Document(str(path))
    except Exception:
        raise SealError("docx_invalid") from None
    parts = []
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            text = compact(paragraph.text)
            if not text:
                continue
            style = ((paragraph.style.name if paragraph.style else "") or "").lower()
            level = re.match(r"heading\s*([1-6])", style)
            if style == "title":
                parts.append("# " + text)
            elif level:
                parts.append("#" * int(level[1]) + " " + text)
            else:
                parts.append(text)
        elif child.tag.endswith("}tbl"):
            rows = []
            for row in Table(child, document).rows:
                cells = [compact(cell.text).replace("|", "\\|") for cell in row.cells]
                # Word represents horizontal merges as repeated cell objects.
                cells = [c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]]
                rows.append(cells)
            if rows:
                parts.append(table_markdown(rows))
    value = "\n\n".join(parts).strip()
    if len(compact(value)) < 8:
        raise SealError("docx_text_unusable")
    return value + "\n"


def safe_member(name):
    parts = [p for p in Path(name.replace("\\", "/")).parts if p not in ("", ".", "/")]
    if not parts or any(p == ".." for p in parts) or name.startswith(("/", "\\")):
        return None
    return "/".join(parts)


def unpack_zip(path, folder, max_bytes):
    """Inner real documents with lineage paths; never hand the outer container to LibreOffice."""
    members = []
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [i for i in archive.infolist() if not i.is_dir()]
            if len(infos) > MAX_ZIP_MEMBERS:
                raise SealError("zip_member_limit_exceeded")
            if sum(i.file_size for i in infos) > max_bytes:
                raise SealError("zip_expanded_size_exceeded")
            for index, info in enumerate(sorted(infos, key=lambda i: i.filename)):
                name = safe_member(info.filename)
                if name is None:
                    raise SealError("zip_member_path_invalid")
                data = archive.read(info)
                kind = detect(data)
                if kind not in ZIP_MEMBER_KINDS and kind != "zip":
                    continue
                target = Path(folder) / f"member-{index}"
                target.write_bytes(data)
                members.append({"path": name, "file": target, "kind": kind})
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        raise SealError("zip_invalid") from None
    if not members:
        raise SealError("zip_without_documents")
    return members


def extract_office(path, kind, cfg):
    """Structured direct extraction first (office_text_policy=prefer), LibreOffice fallback."""
    timeout = cfg["libreoffice_timeout_seconds"]
    prefer = cfg["office_text_policy"] == "prefer"
    info = {"kind": kind, "legacy_encoding": None, "legacy_fonts": [], "attempts": []}
    if kind in {"xls", "xlsx"}:
        markdown, info["extractor"] = extract_excel(path, kind)
        return markdown, info
    if kind == "docx":
        if prefer:
            markdown, info["extractor"] = extract_docx(path), "python_docx"
        else:
            markdown = legal_markdown(extract_libreoffice_text(path, timeout, kind))
            info["extractor"] = "libreoffice_text"
        return markdown, info
    raw = Path(path).read_bytes()
    if kind == "rtf":
        markdown = None
        if prefer:
            try:
                markdown, info["extractor"] = extract_rtf_plain_text(raw), "rtf_direct_text"
            except SealError as exc:
                info["attempts"].append(exc.code)
        if markdown is None:
            markdown, info["extractor"] = (
                extract_libreoffice_text(path, timeout, kind),
                "libreoffice_text",
            )
        uses, fonts = rtf_uses_tcvn3(raw)
        info["legacy_fonts"] = fonts
        # Line-level conversion protects Unicode headers and dates in mixed documents.
        markdown, converted = normalize_detected_tcvn3(markdown, font_evidence=uses)
        if converted:
            info["legacy_encoding"] = "tcvn3"
        return legal_markdown(markdown), info
    if kind == "doc":
        try:
            text, info["extractor"] = (
                extract_libreoffice_text(path, timeout, kind),
                "libreoffice_text",
            )
        except SealError as exc:
            info["attempts"].append(exc.code)
            text = extract_libreoffice_pdf_text(path, timeout, kind)
            info["extractor"] = "libreoffice_pdf_text"
        text, converted = normalize_detected_tcvn3(text)
        if converted:
            info["legacy_encoding"] = "tcvn3"
        return legal_markdown(text), info
    raise SealError("unsupported_office_type")
