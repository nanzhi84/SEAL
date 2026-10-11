"""Independent raw-object, SQL and business population verification of real exports."""

import argparse
import hashlib
import json
import os
import re
from io import StringIO
from pathlib import Path
from urllib.parse import urljoin

import psycopg
import xlrd
from parsel import Selector


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    objects = Path(os.environ["SEAL_ARCHIVE"]) / "objects"
    checks = []
    cache = {}

    def check(name, actual, expected):
        checks.append(
            {
                "name": name,
                "status": "PASS"
                if type(actual) is type(expected) and actual == expected
                else "FAIL",
                "actual": actual,
                "expected": expected,
            }
        )

    def raw(h):
        if h not in cache:
            body = (objects / h[:2] / h[2:]).read_bytes()
            check("object:" + h, hashlib.sha256(body).hexdigest(), h)
            cache[h] = body
        return cache[h]

    def located(snapshot, locator):
        body = raw(snapshot["body_hash"])
        kind = locator["kind"]
        if kind == "response_url":
            return snapshot["url"]
        if kind == "json":
            value = json.loads(body)
            for part in locator["pointer"].split("/")[1:]:
                part = part.replace("~1", "/").replace("~0", "~")
                value = value[int(part)] if type(value) is list else value[part]
            return value
        text = body.decode(snapshot.get("encoding") or "utf-8")
        if kind == "text":
            return text[locator["start"] : locator["end"]].strip()
        if kind == "xpath":
            nodes = Selector(text=text).xpath(locator["path"])
            if len(nodes) != 1:
                raise ValueError("ambiguous locator")
            return " ".join(nodes[0].xpath(".//text() | self::text()").getall()).strip()
        raise ValueError("unsupported independent locator:" + kind)

    with psycopg.connect(os.environ["SEAL_DATABASE_URL"]) as connection:
        for directory in args.evidence:
            inspected = json.loads((directory / "run.json").read_text())
            exported = json.loads((directory / "export.json").read_text())
            run = inspected["run"]
            records = exported["records"]
            tag = directory.name + ":" + run["id"]
            check(tag + ":complete", run["status"], "complete")
            count = connection.execute(
                "SELECT count(DISTINCT record_id) FROM seal_record_emission WHERE run_id=%s AND attempt_epoch=%s",
                (run["id"], run["attempt_epoch"]),
            ).fetchone()[0]
            check(tag + ":sql_emissions", count, len(records))
            snapshots = {}
            for entry in run["inputs"]:
                snapshots[entry["snapshot_id"]] = json.loads(raw(entry["snapshot_id"]))
                raw(snapshots[entry["snapshot_id"]]["body_hash"])
            for record in records:
                candidate = record.get("candidate", record)
                # Export nests the immutable accepted projection under result.
                if "data" not in candidate:
                    candidate = record["result"]["candidate"]
                sid = candidate.get("snapshot_id", candidate.get("primary_snapshot_id"))
                for name, expected in candidate["data"].items():
                    locator = candidate["locators"][name]
                    if locator["kind"] == "xls":
                        if name != "worksheets":
                            continue
                        snapshot = snapshots[sid]
                        book = xlrd.open_workbook(
                            file_contents=raw(snapshot["body_hash"]),
                            formatting_info=True,
                            logfile=StringIO(),
                        )
                        check(tag + ":workbook_sheets", len(expected), book.nsheets)
                        for index, projected in enumerate(expected):
                            sheet = book.sheet_by_index(index)
                            check(tag + ":sheet_name", projected["name"], sheet.name)
                            check(tag + ":sheet_hidden", projected["hidden"], sheet.visibility)
                            check(
                                tag + ":sheet_merges",
                                projected["merged_cells"],
                                [list(x) for x in sheet.merged_cells],
                            )
                            for row in range(sheet.nrows):
                                for col in range(sheet.ncols):
                                    cell = sheet.cell(row, col)
                                    value = cell.value
                                    if cell.ctype in (0, 6):
                                        value = None
                                    elif cell.ctype == 3:
                                        value = xlrd.xldate_as_datetime(
                                            value, book.datemode
                                        ).isoformat()
                                    elif cell.ctype == 4:
                                        value = bool(value)
                                    actual = (
                                        projected["rows"][row][col]
                                        if row < len(projected["rows"])
                                        and col < len(projected["rows"][row])
                                        else None
                                    )
                                    check(tag + f":cell:{index}:{row}:{col}", actual, value)
                        matrix = [
                            row
                            for sheet in candidate["data"]["worksheets"]
                            for row in sheet["rows"]
                        ]
                        first_text = next(
                            cell.strip()
                            for row in matrix
                            for cell in row
                            if type(cell) is str and cell.strip()
                        )
                        text = "\n".join(
                            "\t".join(
                                ""
                                if cell is None
                                else "true"
                                if cell is True
                                else "false"
                                if cell is False
                                else str(cell)
                                for cell in row
                            ).rstrip("\t")
                            for row in matrix
                            if any(cell is not None and cell != "" for cell in row)
                        )
                        check(tag + ":xls_title", candidate["data"]["title"], first_text)
                        check(tag + ":xls_body", candidate["data"]["body"], text)
                        book.release_resources()
                        continue
                    target = snapshots[locator.get("snapshot_id", sid)]
                    check(
                        tag + ":field:" + candidate["record_key"] + ":" + name,
                        located(target, locator),
                        expected,
                    )
            if directory.name in {"dd-017", "dd-041"}:
                is_court = directory.name == "dd-017"
                lists = [
                    s
                    for sid, s in snapshots.items()
                    if any(e["snapshot_id"] == sid and e["role"] == "list" for e in run["inputs"])
                    and s["status"] == 200
                ]
                links = set()
                for snap in lists:
                    html = Selector(body=raw(snap["body_hash"]))
                    css = (
                        '.sec_list a[href*="/shenpan/xiangqing/"]'
                        if is_court
                        else ".commonList_con .li_line a[href]"
                    )
                    links.update(
                        urljoin(snap["url"], n.attrib["href"]).split("#", 1)[0]
                        for n in html.css(css)
                    )
                check(
                    tag + ":all_list_links", sorted(r["record_key"] for r in records), sorted(links)
                )
                page_totals = set()
                for snap in lists:
                    text = raw(snap["body_hash"]).decode(snap.get("encoding") or "utf-8")
                    if is_court:
                        tails = Selector(text=text).css(".page a[href]")
                        for tail in tails:
                            if tail.xpath("string(.)").get().strip() == "尾页":
                                match = re.search(r"/77(?:_([0-9]+))?\.html", tail.attrib["href"])
                                page_totals.add(int(match[1] or "1"))
                    else:
                        matches = re.findall(r"createPageHTML\('page_div',\s*([0-9]+)", text)
                        page_totals.update(map(int, matches))
                check(tag + ":stable_native_page_total", len(page_totals), 1)
                check(tag + ":all_list_pages", len(lists), next(iter(page_totals)))
                attachments = []
                for record in records:
                    snap = snapshots[record["primary_snapshot_id"]]
                    html = Selector(body=raw(snap["body_hash"]))
                    css = ".detail .txt_txt a[href]" if is_court else "#fontzoom a[href]"
                    attachments.extend(
                        n.attrib["href"]
                        for n in html.css(css)
                        if re.search(
                            r"\.(pdf|xls|xlsx|doc|docx|txt)(?:[?#]|$)", n.attrib["href"], re.I
                        )
                    )
                check(tag + ":no_uncollected_business_attachments", attachments, [])
            if directory.name == "dd-102":
                lists = [
                    s
                    for s in snapshots.values()
                    if "/www/illegal" in s["url"]
                    and "listQuery" not in s["url"]
                    and s["status"] == 200
                ]
                raw_keys, raw_details = set(), set()
                for snap in lists:
                    html = Selector(body=raw(snap["body_hash"]))
                    raw_keys.update(
                        n.xpath("string(.)").get().strip()
                        for n in html.css(".cfxx_jg table tr:has(td) td:nth-child(4)")
                    )
                    raw_details.update(
                        urljoin(snap["url"], n.attrib["href"])
                        for n in html.css(".cfxx_jg table td a[href]")
                    )
                native_totals = {
                    int(Selector(body=raw(snap["body_hash"])).css("#total::text").get())
                    for snap in lists
                }
                check(tag + ":stable_penalty_page_total", len(native_totals), 1)
                check(tag + ":all_penalty_pages", len(lists), next(iter(native_totals)))
                check(
                    tag + ":penalty_keys",
                    sorted(r["record_key"] for r in records if r["record_type"].endswith("_row")),
                    sorted(raw_keys),
                )
                check(
                    tag + ":all_penalty_details",
                    sorted(
                        r["record_key"] for r in records if r["record_type"].endswith("_detail")
                    ),
                    sorted(raw_details),
                )
            if directory.name == "dd-468":
                notice = next(r for r in records if r["record_type"].endswith("_notice"))
                snap = snapshots[notice["primary_snapshot_id"]]
                files = {
                    urljoin(snap["url"], n.attrib["href"])
                    for n in Selector(body=raw(snap["body_hash"])).css(".TRS_Editor a[href]")
                    if n.attrib["href"].endswith(".xls")
                }
                workbooks = [r for r in records if r["record_type"].endswith("_workbook")]
                check(
                    tag + ":all_workbooks",
                    sorted(r["record_key"] for r in workbooks),
                    sorted(files),
                )
                counts = sorted(
                    sum(
                        type(row[0]) is float and row[0].is_integer() and bool(row[1])
                        for sheet in r["data"]["worksheets"]
                        for row in sheet["rows"]
                        if len(row) > 2
                    )
                    for r in workbooks
                )
                declared_counts = sorted(
                    map(
                        int,
                        re.findall(
                            r"(?:普通|成人)高等学校\s*([0-9]+)\s*所", notice["data"]["body"]
                        ),
                    )
                )
                check(tag + ":two_declared_school_populations", len(declared_counts), 2)
                check(tag + ":school_population", counts, declared_counts)
            if directory.name in {"dd-250", "dd-247"}:
                payloads = [
                    json.loads(raw(s["body_hash"]))["data"]["data"]
                    for s in snapshots.values()
                    if "/portal/front/" in s["url"] and s["status"] == 200
                ]
                totals = {p["total"] for p in payloads}
                check(tag + ":stable_total", len(totals), 1)
                total = next(iter(totals))
                check(tag + ":population", len(records), total)
                check(tag + ":page_count", len(payloads), (total + 9) // 10)
                check(tag + ":raw_rows", sum(len(p["dataList"]) for p in payloads), total)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "assertions.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2))
    summary = {
        "assertions": len(checks),
        "failed": sum(c["status"] != "PASS" for c in checks),
        "scope": "Fresh SQL and immutable object hashes and locators; Six reviewed business Resources only; national permits and whole-domain completeness excluded",
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))
    return int(bool(summary["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
