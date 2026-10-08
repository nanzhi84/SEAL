"""PDF handling for the Scrapy-side experiment: pypdf (mature library).

Records page-level locators (page number + character span) so extracted text can
be traced back to the original page, which SEAL needs for Field Evidence.
"""
from __future__ import annotations

import io
import re
import time


def parse_pdf(data: bytes) -> dict:
    from pypdf import PdfReader

    t0 = time.perf_counter()
    out = {
        "page_count": None,
        "pages": [],
        "full_text": "",
        "extraction_seconds": None,
        "error": None,
        "parser": "pypdf",
    }
    try:
        reader = PdfReader(io.BytesIO(data))
        out["page_count"] = len(reader.pages)
        meta = reader.metadata or {}
        out["metadata"] = {k: str(v) for k, v in meta.items()}
        chunks = []
        cursor = 0
        for i, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            out["pages"].append({
                "page": i,
                "char_span": [cursor, cursor + len(text)],
                "chars": len(text),
                "text": text,
            })
            # full_text joins pages with a newline; the locator must include it.
            cursor += len(text) + 1
            chunks.append(text)
        out["full_text"] = "\n".join(chunks)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["extraction_seconds"] = round(time.perf_counter() - t0, 3)
    return out


def table_rows(full_text: str) -> list[list[str]]:
    """Best-effort reconstruction of the fixed 3-column table present in the
    NPPA SID list (序号 / 单位名称 / SID码号段). pypdf returns one row per line."""
    rows = []
    for line in full_text.split("\n"):
        m = re.match(r"^\s*(\d{1,3})\s+(.+?)\s+(IFPI\s+[A-Z]{1,3}[\d,\-]+)\s*$", line)
        if m:
            rows.append([m.group(1), m.group(2).strip(), m.group(3).strip()])
    return rows


def page_for_span(pages: list[dict], index: int) -> int | None:
    for p in pages:
        a, b = p["char_span"]
        if a <= index < b:
            return p["page"]
    return None