"""Recompute real business evidence offline, without importing SEAL resolvers."""

import argparse
import hashlib
import json
from datetime import date
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zipfile import ZipFile

import xlrd
from lxml import etree, html
from pypdf import PdfReader


def object_bytes(archive, key):
    data = (archive / "objects" / key[:2] / key[2:]).read_bytes()
    if hashlib.sha256(data).hexdigest() != key:
        raise ValueError("archive_digest_mismatch")
    return data


def pointer(value, path):
    for token in path[1:].split("/") if path else []:
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def canonical_url(value):
    parts = urlsplit(value)
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path or "/",
            urlencode(parse_qsl(parts.query, keep_blank_values=True)),
            "",
        )
    )


@lru_cache(maxsize=4)
def pdf_pages(body):
    # Immutable public bytes; bounded cache avoids re-reading a long PDF for
    # every field/page locator while retaining strict parser diagnostics.
    return [
        (page.extract_text() or "").strip() for page in PdfReader(BytesIO(body), strict=True).pages
    ]


@lru_cache(maxsize=4)
def workbook(body):
    from io import StringIO

    book = xlrd.open_workbook(file_contents=body, formatting_info=True, logfile=StringIO())
    output = []
    try:
        for sheet in book.sheets():
            rows, height, width = [], 0, 0
            for r in range(sheet.nrows):
                row = []
                for c in range(sheet.ncols):
                    cell = sheet.cell(r, c)
                    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        value = None
                    elif cell.ctype == xlrd.XL_CELL_DATE:
                        value = xlrd.xldate_as_datetime(cell.value, book.datemode).isoformat()
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        value = bool(cell.value)
                    elif cell.ctype in (xlrd.XL_CELL_NUMBER, xlrd.XL_CELL_TEXT):
                        value = cell.value
                    else:
                        raise ValueError("xls_cell_error")
                    row.append(value)
                    if value is not None and value != "":
                        height, width = max(height, r + 1), max(width, c + 1)
                rows.append(row)
            for _, rhi, _, chi in sheet.merged_cells:
                height, width = max(height, rhi), max(width, chi)
            output.append(
                {
                    "name": sheet.name,
                    "hidden": sheet.visibility,
                    "rows": [row[:width] for row in rows[:height]],
                    "merged_cells": [list(bounds) for bounds in sheet.merged_cells],
                }
            )
    finally:
        book.release_resources()
    return output


WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def word_blocks(container):
    result = []
    for node in container:
        if node.tag == WORD + "p":
            fragments = []
            for direct in node:
                runs = (
                    [direct]
                    if direct.tag == WORD + "r"
                    else (direct.findall(WORD + "r") if direct.tag == WORD + "hyperlink" else [])
                )
                for run in runs:
                    for child in run:
                        tag = child.tag
                        if tag == WORD + "t":
                            fragments.append(child.text or "")
                        elif tag in {WORD + "tab", WORD + "ptab"}:
                            fragments.append("\t")
                        elif tag == WORD + "noBreakHyphen":
                            fragments.append("-")
                        elif tag == WORD + "cr" or (
                            tag == WORD + "br"
                            and child.get(WORD + "type", "textWrapping") == "textWrapping"
                        ):
                            fragments.append("\n")
            text = "".join(fragments)
            result.append({"text": text})
        elif node.tag == WORD + "tbl":
            rows = []
            for row in node.findall(WORD + "tr"):

                def prop(name, default, row=row):
                    found = row.find(WORD + "trPr/" + WORD + name)
                    return int(found.get(WORD + "val", default)) if found is not None else default

                before, after = prop("gridBefore", 0), prop("gridAfter", 0)
                cells = []
                for cell in row.findall(WORD + "tc"):
                    span = cell.find(WORD + "tcPr/" + WORD + "gridSpan")
                    span = int(span.get(WORD + "val")) if span is not None else 1
                    merge = cell.find(WORD + "tcPr/" + WORD + "vMerge")
                    merge = merge.get(WORD + "val", "continue") if merge is not None else None
                    data = {"blocks": word_blocks(cell)}
                    if span != 1:
                        data["span"] = span
                    if merge is not None:
                        data["vertical_merge"] = merge
                    cells.append(data)
                data = {"cells": cells}
                if before:
                    data["before"] = before
                if after:
                    data["after"] = after
                rows.append(data)
            grid = node.find(WORD + "tblGrid")
            result.append({"kind": "table", "columns": len(grid), "rows": rows})
    return result


@lru_cache(maxsize=4)
def word_document(body):
    with ZipFile(BytesIO(body)) as package:
        # Separate OOXML traversal does not import Runtime/python-docx resolvers.
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        document = etree.fromstring(package.read("word/document.xml"), parser)
    return word_blocks(document.find(WORD + "body"))


def word_text(blocks):
    lines = []
    for block in blocks:
        if "text" in block:
            if block["text"].strip():
                lines.append(block["text"].strip())
        else:
            for row in block["rows"]:
                cells = [word_text(cell["blocks"]) for cell in row["cells"]]
                if any(cells):
                    lines.append("\t".join(cells))
    return "\n".join(lines)


def attachment_value(body, locator):
    selection = locator["selection"]
    if selection == "coverage":
        return word_coverage(body)
    structure = workbook(body) if locator["kind"] == "xls" else word_document(body)
    if selection == "structure":
        return structure
    if locator["kind"] == "xls":

        def cell_text(value):
            if value is None:
                return ""
            return str(value).lower() if type(value) is bool else str(value)

        lines = [
            "\t".join(map(cell_text, row)).rstrip("\t")
            for sheet in structure
            for row in sheet["rows"]
            if any(cell is not None and cell != "" for cell in row)
        ]
        title = next(
            cell.strip()
            for sheet in structure
            for row in sheet["rows"]
            for cell in row
            if type(cell) is str and cell.strip()
        )
        text = "\n".join(lines)
    else:
        text = word_text(structure)
        title = text.splitlines()[0].strip()
    if selection == "title":
        return title
    if selection == "text":
        return text
    raise ValueError("invalid_attachment_locator")


def word_coverage(body):
    names = {
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
    }
    math = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
    unsupported = {WORD + name for name in names} | {math + "oMath", math + "oMathPara"}
    counts, parts = {}, []
    with ZipFile(BytesIO(body)) as package:
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        doc = etree.fromstring(package.read("word/document.xml"), parser)
        main = doc.find(WORD + "body")
        for node in main.iter():
            if node.tag in unsupported:
                name = node.tag.rsplit("}", 1)[-1]
                counts[name] = counts.get(name, 0) + 1
        for node in main:
            if node.tag not in {WORD + "p", WORD + "tbl", WORD + "sectPr"}:
                name = node.tag.rsplit("}", 1)[-1]
                if name not in counts:
                    counts[name] = 1
        for path in package.namelist():
            if (
                path.startswith("word/")
                and path.endswith(".xml")
                and path.split("/")[-1].startswith(("header", "footer", "footnotes", "endnotes"))
            ):
                root = etree.fromstring(package.read(path), parser)
                count = sum(bool(node.text and node.text.strip()) for node in root.iter(WORD + "t"))
                if count:
                    parts.append({"part": path, "text_nodes": count})
    return {
        "scope": "main_body_paragraphs_tables",
        "unparsed_elements": counts,
        "non_body_parts": sorted(parts, key=lambda item: item["part"]),
    }


def located(snapshot, body, locator):
    kind = locator["kind"]
    encoding = snapshot.get("encoding") or "utf-8"
    if kind == "json":
        value = pointer(json.loads(body), locator["pointer"])
    elif kind == "response_url":
        value = canonical_url(snapshot["url"])
    elif kind == "text":
        value = body.decode(encoding)[locator["start"] : locator["end"]].strip()
    elif kind == "xpath":
        nodes = html.document_fromstring(body.decode(encoding)).xpath(locator["path"])
        if len(nodes) != 1:
            raise ValueError("ambiguous_xpath")
        node = nodes[0]
        value = " ".join(node.xpath(".//text()") if hasattr(node, "xpath") else [str(node)]).strip()
    elif kind == "pdf":
        text = pdf_pages(body)[locator["page"] - 1]
        value = text[locator["start"] : locator["end"]].strip()
    elif kind == "segments":
        value = locator.get("separator", "\n").join(
            located(snapshot, body, part) for part in locator["segments"]
        )
    elif kind in ("xls", "docx"):
        value = attachment_value(body, locator)
    else:
        raise ValueError("unsupported_locator:" + kind)
    if locator.get("transform") == "key_string":
        value = str(value)
    elif locator.get("transform") == "date_iso":
        value = date.fromisoformat(value).isoformat()
    return value


def same(left, right):
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(
        right, sort_keys=True, ensure_ascii=False
    )


def verify_export(archive, exported):
    errors = []
    for record in exported["records"]:
        try:
            snapshots = {}
            for ref in record["inputs"]:
                snapshot = json.loads(object_bytes(archive, ref["snapshot_id"]))
                body = object_bytes(archive, snapshot["body_hash"])
                if snapshot["body_hash"] != ref["body_hash"] or snapshot["body_size"] != len(body):
                    raise ValueError("body_reference_mismatch")
                snapshots[ref["snapshot_id"]] = snapshot, body
            if not snapshots:
                raise ValueError("no_raw_evidence")
            if not record["result_evidence"]:
                raise ValueError("no_result_evidence")
            for result in record["result_evidence"]:
                primary = result["primary_snapshot_id"]
                if primary not in snapshots:
                    raise ValueError("primary_snapshot_missing")
                if set(result["locators"]) != set(record["data"]):
                    raise ValueError("incomplete_field_locators")
                snapshot = snapshots[primary][0]
                if snapshot["method"] != "GET" or snapshot["status"] != 200:
                    raise ValueError("invalid_business_input")
                if not result["primary_inputs"] or not any(
                    ref["snapshot_id"] == primary for ref in result["primary_inputs"]
                ):
                    raise ValueError("primary_observation_missing")
                for field, locator in result["locators"].items():
                    pair = snapshots[locator.get("snapshot_id", primary)]
                    if not same(located(*pair, locator), record["data"][field]):
                        raise ValueError("field_mismatch:" + field)
                locator = result["key_locator"]
                if (
                    located(*snapshots[locator.get("snapshot_id", primary)], locator)
                    != record["record_key"]
                ):
                    raise ValueError("key_mismatch")
        except Exception as exc:
            errors.append({"record_key": record["record_key"], "error": str(exc)})
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--public",
        action="store_true",
        help="Verify disclosed inputs; explicitly report private raw Sources",
    )
    args = parser.parse_args()
    root = args.directory.resolve()
    manifest = json.loads(root.joinpath("manifest.json").read_text())
    errors, files, records = [], 0, 0
    private = manifest.get("local_only_source_ids", [])
    if private and not args.public:
        errors.append({"error": "private_raw_requires_full_local_archive"})
    for name, expected in manifest["files"].items():
        path = (root / name).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            errors.append({"file": name, "error": "artifact_hash_mismatch"})
        files += 1
    assertions = json.loads(root.joinpath("assertions.json").read_text())
    for item in assertions:
        if item["actual"] != item["expected"] or item["status"] != "PASS":
            errors.append({"assertion": item["name"], "error": "failed_assertion"})
    for path in root.glob("*.json"):
        value = json.loads(path.read_text())
        if isinstance(value, dict) and "export" in value:
            exported = value["export"]
            if args.public and exported["source_id"] in private:
                continue
            records += len(exported["records"])
            errors.extend(verify_export(root / "archive", exported))
    print(
        json.dumps(
            {
                "files": files,
                "record_projections": records,
                "assertions": len(assertions),
                "errors": errors,
                "local_only_sources": private,
            },
            ensure_ascii=False,
        )
    )
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
