"""Deterministic XLS/DOCX projections of immutable public attachment bytes.

The output describes a document, never entities identified by table position.
Merged-cell topology and empty grid positions stay visible in structured data.
Unsupported content fails explicitly; callers retain the already archived input.
"""

import math
from io import BytesIO, StringIO
from zipfile import ZipFile

import xlrd
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from lxml import etree

from .core import SealError

MAX_CELLS = 500000
MAX_DOCX_EXPANDED_BYTES = 64 * 1024 * 1024


def _xls_cell(book, cell):
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cell.value, book.datemode).isoformat()
        except (ValueError, OverflowError, xlrd.XLDateError):
            raise SealError("xls_invalid_date") from None
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        if not math.isfinite(cell.value):
            raise SealError("xls_nonfinite_number")
        return cell.value
    if cell.ctype == xlrd.XL_CELL_TEXT:
        return cell.value
    raise SealError("xls_cell_error")


def xls_structure(body):
    book = None
    try:
        book = xlrd.open_workbook(
            file_contents=body, formatting_info=True, logfile=StringIO(), on_demand=True
        )
        sheets, total = [], 0
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            total += sheet.nrows * sheet.ncols
            if total > MAX_CELLS:
                raise SealError("xls_cell_budget_exceeded")
            rows = [
                [_xls_cell(book, sheet.cell(row, col)) for col in range(sheet.ncols)]
                for row in range(sheet.nrows)
            ]
            used = [
                (r + 1, c + 1)
                for r, row in enumerate(rows)
                for c, cell in enumerate(row)
                if cell is not None and cell != ""
            ]
            # A merge can extend beyond its anchor's populated cell. Keep the
            # full layout range; formatting-only trailing margins are excluded.
            used.extend((rhi, chi) for _, rhi, _, chi in sheet.merged_cells)
            height = max((r for r, _ in used), default=0)
            width = max((c for _, c in used), default=0)
            if height * width + total - sheet.nrows * sheet.ncols > MAX_CELLS:
                raise SealError("xls_cell_budget_exceeded")
            while len(rows) < height:
                rows.append([])
            sheets.append(
                {
                    "name": sheet.name,
                    "hidden": sheet.visibility,
                    "rows": [
                        (row + [None] * max(0, width - len(row)))[:width] for row in rows[:height]
                    ],
                    "merged_cells": [list(bounds) for bounds in sheet.merged_cells],
                }
            )
            book.unload_sheet(index)
        return sheets
    except SealError:
        raise
    except Exception:
        raise SealError("xls_parse_failed") from None
    finally:
        if book is not None:
            book.release_resources()


def _docx_blocks(container, depth=0):
    if depth > 16:
        raise SealError("docx_structure_too_deep")
    blocks = []
    for part in container.iter_inner_content():
        if isinstance(part, Paragraph):
            # Paragraph is the default block shape; retaining empty paragraphs
            # costs one field and keeps large classification tables bounded.
            blocks.append({"text": part.text})
        elif isinstance(part, Table):
            rows = []
            for row in part.rows:
                cells = []
                # Row.cells repeats merged anchors. Actual XML cells preserve
                # horizontal spans and vertical continuations without copying.
                for node in row._tr.tc_lst:
                    cell = {"blocks": _docx_blocks(_Cell(node, part), depth + 1)}
                    if node.grid_span != 1:
                        cell["span"] = node.grid_span
                    if node.vMerge is not None:
                        cell["vertical_merge"] = node.vMerge
                    cells.append(cell)
                row_data = {"cells": cells}
                if row.grid_cols_before:
                    row_data["before"] = row.grid_cols_before
                if row.grid_cols_after:
                    row_data["after"] = row.grid_cols_after
                rows.append(row_data)
            blocks.append({"kind": "table", "columns": len(part.columns), "rows": rows})
    return blocks


def _docx_document(body):
    try:
        with ZipFile(BytesIO(body)) as package:
            entries = package.infolist()
            if len(entries) > 2000 or sum(entry.file_size for entry in entries) > (
                MAX_DOCX_EXPANDED_BYTES
            ):
                raise SealError("docx_expansion_budget_exceeded")
        return Document(BytesIO(body))
    except SealError:
        raise
    except Exception:
        raise SealError("docx_parse_failed") from None


def docx_coverage(body):
    document = _docx_document(body)
    try:
        # python-docx does not expose these as ordinary paragraph/table text.
        # Report the boundary instead of silently emitting an incomplete body.
        unsupported = {
            qn("w:" + name)
            for name in (
                "altChunk",
                "drawing",
                "pict",
                "object",
                "sdt",
                "ins",
                "del",
                "moveFrom",
                "moveTo",
                "footnoteReference",
                "endnoteReference",
                "customXml",
                "fldSimple",
            )
        }
        unsupported.update({qn("m:oMath"), qn("m:oMathPara")})
        counts = {}
        for node in document.element.body.iter():
            if node.tag in unsupported:
                name = node.tag.rsplit("}", 1)[-1]
                counts[name] = counts.get(name, 0) + 1
        for node in document.element.body:
            if node.tag not in {qn("w:p"), qn("w:tbl"), qn("w:sectPr")}:
                name = node.tag.rsplit("}", 1)[-1]
                if name not in counts:
                    counts[name] = 1
        parts = []
        with ZipFile(BytesIO(body)) as package:
            for path in package.namelist():
                if (
                    path.startswith("word/")
                    and path.endswith(".xml")
                    and path.split("/")[-1].startswith(
                        ("header", "footer", "footnotes", "endnotes")
                    )
                ):
                    parser = etree.XMLParser(resolve_entities=False, no_network=True)
                    root = etree.fromstring(package.read(path), parser)
                    count = sum(
                        bool(node.text and node.text.strip()) for node in root.iter(qn("w:t"))
                    )
                    if count:
                        parts.append({"part": path, "text_nodes": count})
        return {
            "scope": "main_body_paragraphs_tables",
            "unparsed_elements": counts,
            "non_body_parts": sorted(parts, key=lambda item: item["part"]),
        }
    except SealError:
        raise
    except Exception:
        raise SealError("docx_parse_failed") from None


def docx_structure(body):
    try:
        return _docx_blocks(_docx_document(body))
    except SealError:
        raise
    except Exception:
        raise SealError("docx_parse_failed") from None


def _cell_text(value):
    if value is None:
        return ""
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def docx_text(blocks):
    lines = []
    for block in blocks:
        if "text" in block:
            if block["text"].strip():
                lines.append(block["text"].strip())
        else:
            for row in block["rows"]:
                cells = [docx_text(cell["blocks"]) for cell in row["cells"]]
                if any(cells):
                    lines.append("\t".join(cells))
    return "\n".join(lines)


def attachment_projection(kind, structure, selection):
    if selection == "structure":
        return structure
    if kind == "xls":
        lines = [
            "\t".join(_cell_text(cell) for cell in row).rstrip("\t")
            for sheet in structure
            for row in sheet["rows"]
            if any(cell is not None and cell != "" for cell in row)
        ]
        title = next(
            (
                cell.strip()
                for sheet in structure
                for row in sheet["rows"]
                for cell in row
                if type(cell) is str and cell.strip()
            ),
            "",
        )
        text = "\n".join(lines)
    else:
        text = docx_text(structure)
        title = text.splitlines()[0].strip() if text else ""
    if not text or not title:
        raise SealError("empty_document")
    if selection == "text":
        return text
    if selection == "title":
        return title
    raise SealError("invalid_attachment_locator")


def parse_attachment(kind, body):
    return xls_structure(body) if kind == "xls" else docx_structure(body)


def located_attachment(body, locator, *, structure=None):
    if set(locator) - {"kind", "selection", "snapshot_id"} or locator.get("selection") not in (
        "structure",
        "text",
        "title",
        "coverage",
    ):
        raise SealError("invalid_attachment_locator")
    kind = locator["kind"]
    if locator["selection"] == "coverage":
        if kind != "docx":
            raise SealError("invalid_attachment_locator")
        return docx_coverage(body)
    if structure is None:
        structure = parse_attachment(kind, body)
    return attachment_projection(kind, structure, locator["selection"])
