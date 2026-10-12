"""Bounded XLS and DOCX format readers used by the unified parser."""

import math
from io import BytesIO, StringIO
from zipfile import ZipFile

import xlrd
from docx import Document

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
