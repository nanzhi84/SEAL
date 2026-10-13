#!/usr/bin/env python3
"""Independent archive/SQL/SDK oracle. Never imports seal or reads its source.

Failure modes declared before the implementation under acceptance is exercised:
- New run receipts accidentally point to historical or another Source's input.
- A successful or failed SDK result disappears after the process exits.
- CAS bytes/digests, Observation input, Version origin, or diagnostic keys disagree.
- Failed/unsupported/blocked inputs advance the successful Version timeline.
- Repeated delivery creates duplicate run/URL observations or crosses input ownership.
- HTTP success, nonempty Markdown, or page counts hide missing native content.
- Discovery misses/duplicates court details, PDFs, AMAC pages, or notice XLS files.
- JSON/XLS unavailable semantics are misreported as content acceptance.

Run after an isolated real CLI collection, reusing its archives rather than fetching again:
uv run --frozen python tests/playground_crawlresult_audit.py --archive ARCHIVE \
  --receipt dd-017=RECEIPT --output artifacts/RUN/independent
SEAL_DATABASE_URL must address only the isolated acceptance database.
The public run.report.crawl_results map is Observation ID -> CAS reference.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import ipaddress
import json
import math
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urldefrag, urljoin, urlparse

import psycopg
from crawl4ai import CrawlResult
from lxml import html as lhtml
from psycopg import sql
from psycopg.rows import dict_row


def plain(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plain(value), ensure_ascii=False, indent=2) + "\n")


class Audit:
    def __init__(self, archive: Path, output: Path):
        self.archive, self.output = archive, output
        self.assertions, self.cas_evidence = [], {}

    def check(self, name, condition, evidence=None):
        self.assertions.append({"name": name, "status": "PASS" if condition else "FAIL",
                                "evidence": plain(evidence)})
        return bool(condition)

    def cas(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError(f"invalid CAS digest: {digest!r}")
        path = self.archive / "objects" / digest[:2] / digest[2:]
        data = path.read_bytes()
        computed = hashlib.sha256(data).hexdigest()
        self.check(f"CAS {digest}", computed == digest, {"bytes": len(data)})
        self.cas_evidence[digest] = {"bytes": len(data), "sha256": computed}
        return data


def catalog(conn):
    rows = conn.execute("""SELECT c.table_schema,c.table_name,c.column_name FROM
        information_schema.columns c JOIN information_schema.tables t
        ON c.table_schema=t.table_schema AND c.table_name=t.table_name
        WHERE c.table_schema='seal' AND t.table_type='BASE TABLE'
        ORDER BY c.table_name,c.ordinal_position""").fetchall()
    tables = {}
    for row in rows:
        tables.setdefault(row["table_name"], []).append(row["column_name"])
    return tables


def identify(tables, required):
    matches = [name for name, cols in tables.items() if required <= set(cols)]
    if len(matches) != 1:
        raise ValueError(f"catalog cannot identify {sorted(required)}: {matches}")
    return matches[0]


def table_rows(conn, table):
    return conn.execute(sql.SQL("SELECT * FROM seal.{}").format(sql.Identifier(table))).fetchall()


def extract_text(data):
    tree = lhtml.fromstring(data)
    for node in tree.xpath("//script|//style|//noscript"):
        node.drop_tree()
    text = " ".join(tree.text_content().split())
    paragraphs = [" ".join(p.text_content().split()) for p in tree.xpath("//p")]
    return tree, text, [p for p in paragraphs if len(p) >= 50]


def links(tree, base, extension=None, xpath="//a[@href]"):
    result = []
    for node in tree.xpath(xpath):
        href = node.get("href")
        if not href:
            continue
        url = urldefrag(urljoin(base, href))[0]
        if extension and not urlparse(url).path.lower().endswith(extension):
            continue
        if url not in result:
            result.append(url)
    return result


def normalize(value):
    return "".join(re.findall(r"[^\W_]", value))


def markdown_text(value):
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value)


def readable_result(envelope, audit=None):
    if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), dict):
        raise ValueError("CrawlResult archive must contain a result dictionary")
    payload = dict(envelope["result"])
    payload.setdefault("fit_html", None)
    if audit:
        for field, digest in (envelope.get("artifacts") or {}).items():
            data = audit.cas(diagnostic_ref(digest))
            payload[field] = (data if field == "pdf" else base64.b64encode(data).decode()
                              if field == "screenshot" else data.decode())
        downloads = payload.get("downloaded_files") or []
        paths = []
        for digest in downloads:
            audit.cas(digest)
            paths.append(str(audit.archive / "objects" / digest[:2] / digest[2:]))
        if downloads:
            payload["downloaded_files"] = paths
    return CrawlResult.model_validate(payload), payload


def result_summary(result, payload):
    markdown = payload.get("markdown") or {}
    if isinstance(markdown, str):
        raw_markdown = markdown
    else:
        raw_markdown = markdown.get("raw_markdown", "")
    return {"url": result.url, "success": result.success, "status_code": result.status_code,
            "error_message": result.error_message, "html_chars": len(result.html or ""),
            "cleaned_html_chars": len(result.cleaned_html or ""),
            "markdown_chars": len(raw_markdown),
            "links": {k: len(v) for k, v in result.links.items()},
            "media": {k: len(v) for k, v in result.media.items()},
            "tables": len(result.tables), "downloads": result.downloaded_files or [],
            "metadata": result.metadata, "raw_markdown": raw_markdown}


def diagnostic_ref(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for field in ("archive_ref", "crawl_result_ref", "sha256"):
            if field in value:
                return value[field]
    raise ValueError(f"unrecognized diagnostic reference: {value!r}")


def verify_artifacts(audit, envelope, name):
    for field, value in (envelope.get("artifacts") or {}).items():
        items = value if isinstance(value, list) else [value]
        for item in items:
            digest = diagnostic_ref(item)
            audit.cas(digest)
            audit.check(f"{name} durable artifact {field}", True, digest)


def inspect_inputs(audit, source, run, observations, versions):
    diagnostic = (run.get("report") or {}).get("crawl_results", {})
    audit.check(f"{source} diagnostic map exists", isinstance(diagnostic, dict),
                {"type": type(diagnostic).__name__, "entries": len(diagnostic)})
    if not isinstance(diagnostic, dict):
        diagnostic = {}
    observed_ids = {str(o["id"]) for o in observations}
    audit.check(f"{source} diagnostic keys belong to this run", set(diagnostic) <= observed_ids,
                sorted(set(diagnostic) - observed_ids))
    audit.check(f"{source} one observation per run URL",
                len({o["url"] for o in observations}) == len(observations))
    result_rows = []
    for obs in observations:
        oid, url = str(obs["id"]), obs["url"]
        row = {"observation": plain(obs), "result": None, "body_bytes": 0}
        try:
            body = audit.cas(obs["body_hash"]) if obs.get("body_hash") else None
            row["body_bytes"] = len(body or b"")
            if body:
                audit.check(f"{oid} input ref equals body digest", obs.get("archive_ref") == obs["body_hash"])
            policy = obs.get("role") == "policy" or urlparse(url).path == "/robots.txt"
            has_result = oid in diagnostic
            row["crawl_result_available"] = has_result
            if not policy:
                audit.check(f"{oid} SDK result persists", has_result,
                            {"url": url, "outcome": obs.get("outcome"), "error": obs.get("error")})
            if has_result:
                ref = diagnostic_ref(diagnostic[oid])
                envelope = json.loads(audit.cas(ref))
                result, payload = readable_result(envelope, audit)
                summary = result_summary(result, payload)
                row["result"] = summary
                row["crawl_result_ref"] = ref
                row["envelope"] = envelope
                audit.check(f"{oid} SDK URL matches input",
                            result.url in {url, obs.get("actual_url")}, result.url)
                audit.check(f"{oid} SDK status is consistent with observation",
                            result.status_code == obs.get("status") or (
                                result.status_code is None and not result.success and bool(result.error_message)),
                            {"sdk": result.status_code, "observation": obs.get("status")})
                if result.success and body and body.lstrip().startswith((b"<!DOCTYPE", b"<!doctype", b"<html", b"<HTML")):
                    encoding = (re.search(rb"charset=[\"']?([\w-]+)", body[:4096]) or [None, b"utf-8"])[1]
                    raw_html = body.decode(encoding.decode(), errors="replace")
                    audit.check(f"{oid} SDK HTML equals archived input", result.html == raw_html)
                verify_artifacts(audit, envelope, oid)
                dump(audit.output / source / f"{oid}-crawlresult.json", envelope)
            if obs.get("outcome") not in {"parsed", "unchanged"} and not policy:
                audit.check(f"{oid} failed input has no version", obs.get("version_id") is None)
            if obs.get("version_id"):
                vid = str(obs["version_id"])
                version = versions[vid]
                audit.check(f"{oid} Version belongs to input resource", version["resource_id"] == obs["resource_id"])
                audit.check(f"{oid} Version input digest and mode match observation",
                            version["body_hash"] == obs.get("body_hash") and version["input_mode"] == obs.get("input_mode"))
                if obs.get("outcome") == "parsed":
                    audit.check(f"{oid} Version originates in this input", str(version["observation_id"]) == oid)
                    if row.get("envelope"):
                        audit.check(f"{oid} Version and diagnostic have same SDK result",
                                    version["crawl_result"] == row["envelope"])
            if body and not policy and not body.startswith(b"%PDF"):
                try:
                    tree, text, paragraphs = extract_text(body)
                    row["raw_text_chars"], row["raw_paragraphs"] = len(text), paragraphs
                    row["raw_links"] = links(tree, url)
                    next_links = links(tree, url, xpath="//li[contains(@class,'next')]/a[@href]")
                    row["raw_next_page"] = next_links[0] if next_links else None
                    article = tree.xpath("//div[contains(concat(' ',normalize-space(@class),' '),' txt_txt ')]")
                    if article:
                        row["raw_article_text"] = article[0].text_content()
                        row["raw_article_images"] = [urljoin(url, image.get("src")) for image in article[0].xpath('.//img[@src]')]
                        row["raw_article_tables"] = len(article[0].xpath('.//table'))
                except (ValueError, UnicodeError):
                    pass
        except Exception as exc:
            audit.check(f"{oid} readable input and SDK result", False, str(exc))
        result_rows.append(row)
    dump(audit.output / source / f"inputs-{run['id']}.json", result_rows)
    return result_rows


def business(audit, source, rows):
    content = [r for r in rows if urlparse(r["observation"]["url"]).path != "/robots.txt"]
    urls = {r["observation"]["url"] for r in content}
    successful = {r["observation"]["url"] for r in content if r["observation"].get("version_id")}
    summary = {"source": source, "observed_urls": sorted(urls), "successful_urls": sorted(successful),
               "status": "UNVERIFIED", "coverage": {}}
    results = [r["result"] for r in content if r.get("result")]
    summary["crawlresult_fields"] = {"available": len(results), "missing": len(content) - len(results),
        "success": sum(r["success"] for r in results), "html_chars": sum(r["html_chars"] for r in results),
        "markdown_chars": sum(r["markdown_chars"] for r in results),
        "links": sum(sum(r["links"].values()) for r in results),
        "media": sum(sum(r["media"].values()) for r in results),
        "tables": sum(r["tables"] for r in results), "downloads": sum(len(r["downloads"]) for r in results)}
    if source == "dd-017":
        list_rows = [r for r in content if "/shenpan/gengduo/" in r["observation"]["url"]]
        detail_urls = set()
        for row in list_rows:
            detail_urls.update(u for u in row.get("raw_links", []) if "/shenpan/xiangqing/" in u)
        audit.check("dd-017 ten requested list pages", len(list_rows) == 10, len(list_rows))
        by_url = {r["observation"]["url"]: r for r in list_rows}
        current, chain = "https://www.court.gov.cn/shenpan/gengduo/77.html", []
        for _ in range(10):
            if current not in by_url or current in chain:
                break
            chain.append(current)
            current = by_url[current].get("raw_next_page")
        audit.check("dd-017 list URLs follow ten archived next-page links",
                    len(chain) == 10 and set(chain) == set(by_url), chain)
        audit.check("dd-017 archived list detail links are all observed", detail_urls <= urls, sorted(detail_urls - urls))
        audit.check("dd-017 archived list detail links all have successful results", detail_urls <= successful,
                    sorted(detail_urls - successful))
        summary["coverage"] = {"lists": len(list_rows), "declared_details": len(detail_urls),
                               "observed_details": len(detail_urls & urls), "successful_details": len(detail_urls & successful)}
        for row in content:
            if row["observation"]["url"] not in detail_urls:
                continue
            markdown = ((row.get("result") or {}).get("raw_markdown") or "")
            article = row.get("raw_article_text", "")
            audit.check(f"{row['observation']['id']} court full article character sequence correspondence",
                        bool(normalize(article)) and normalize(article) in normalize(markdown_text(markdown)),
                        {"article_chars": len(article), "markdown_chars": len(markdown)})
            for image in row.get("raw_article_images", []):
                audit.check(f"{row['observation']['id']} court content image reference survives", image in markdown, image)
            if row.get("raw_article_tables"):
                audit.check(f"{row['observation']['id']} court content tables survive", (row.get("result") or {}).get("tables", 0) >= row["raw_article_tables"])
        summary["status"] = "PASS" if detail_urls and detail_urls <= successful and len(list_rows) == 10 else "FAIL"
    elif source in {"dd-050", "dd-468"}:
        ext = ".pdf" if source == "dd-050" else ".xls"
        entry = next((r for r in content if not urlparse(r["observation"]["url"]).path.lower().endswith(ext)), None)
        expected = {u for u in (entry or {}).get("raw_links", []) if urlparse(u).path.lower().endswith(ext)}
        audit.check(f"{source} archive declares attachment collection", bool(expected), sorted(expected))
        audit.check(f"{source} all declared attachments observed", expected <= urls, sorted(expected - urls))
        audit.check(f"{source} all declared attachments have content Version", expected <= successful,
                    sorted(expected - successful))
        summary["coverage"] = {"declared_attachments": len(expected), "observed": len(expected & urls),
                               "successful": len(expected & successful)}
        summary["status"] = "PASS" if expected and expected <= successful else "FAIL"
        if source == "dd-050":
            for row in content:
                if row["observation"]["url"] not in expected:
                    continue
                if not row["observation"].get("body_hash"):
                    audit.check(f"{row['observation']['id']} PDF original is archived", False,
                                row["observation"].get("error"))
                    continue
                body = audit.cas(row["observation"]["body_hash"])
                audit.check(f"{row['observation']['id']} PDF magic", body.startswith(b"%PDF"))
                from pypdf import PdfReader
                pdf = PdfReader(io.BytesIO(body))
                native_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
                markdown = ((row.get("result") or {}).get("raw_markdown") or "")
                fragments = [normalize(line) for line in native_text.splitlines() if len(normalize(line)) >= 20]
                normalized_md = normalize(markdown_text(markdown))
                missing = [line for line in fragments if line not in normalized_md]
                audit.check(f"{row['observation']['id']} PDF native text fragment correspondence",
                            bool(fragments) and not missing,
                            {"native_chars": len(native_text), "markdown_chars": len(markdown),
                             "pages": len(pdf.pages), "fragments": len(fragments), "missing": len(missing),
                             "missing_samples": missing[:10]})
                row["pdf_pages"], row["native_text_chars"] = len(pdf.pages), len(native_text)
        else:
            summary["limitation"] = "XLS must be semantically readable and cell faithful; raw fetch alone does not satisfy this case."
            for row in content:
                if row["observation"]["url"] not in expected:
                    continue
                digest = row["observation"].get("body_hash")
                original = audit.cas(digest) if digest else b""
                audit.check(f"{row['observation']['id']} declared XLS has native OLE bytes",
                            original.startswith(bytes.fromhex("D0CF11E0A1B11AE1")),
                            {"bytes": len(original), "outcome": row["observation"].get("outcome")})
    elif source == "dd-247":
        pages = []
        for row in content:
            if "findMutualFundHousePage" not in row["observation"]["url"]:
                continue
            try:
                native = json.loads(audit.cas(row["observation"]["body_hash"]))
                query = parse_qs(urlparse(row["observation"]["url"]).query, keep_blank_values=True)
                data = native["data"]["data"]
                pages.append({"url": row["observation"]["url"], "native": native,
                              "page": int(query["pageNo"][0]), "page_size": int(query["pageSize"][0]),
                              "total": int(data["total"]), "rows": data["dataList"],
                              "successful": bool(row["observation"].get("version_id"))})
                audit.check(f"dd-247 {row['observation']['id']} native empty filters",
                            all(query.get(field) == [""] for field in ("houseName", "registerAddr", "officeAddr")), query)
            except (ValueError, TypeError) as exc:
                audit.check("dd-247 native API response JSON", False, str(exc))
        summary["coverage"] = {"native_api_pages": len(pages)}
        summary["status"] = "FAIL"
        summary["limitation"] = "Requires native empty-filter full pagination and total from this run; entrance HTML is not directory coverage."
        dump(audit.output / source / "native-pages.json", pages)
        audit.check("dd-247 native API pages are present", bool(pages), len(pages))
        if pages:
            total, size = pages[0]["total"], pages[0]["page_size"]
            expected_pages = set(range(1, max(1, math.ceil(total / size)) + 1))
            native_rows = [row for page in pages for row in page["rows"]]
            audit.check("dd-247 native total remains stable", all(p["total"] == total for p in pages))
            audit.check("dd-247 all native pages are present", {p["page"] for p in pages} == expected_pages,
                        {"expected": sorted(expected_pages), "actual": [p["page"] for p in pages]})
            audit.check("dd-247 total equals this run's native row count", len(native_rows) == total,
                        {"total": total, "rows": len(native_rows)})
            audit.check("dd-247 native rows have no duplicate full values",
                        len({json.dumps(r, sort_keys=True, ensure_ascii=False) for r in native_rows}) == len(native_rows))
            summary["coverage"].update({"native_total": total, "expected_pages": len(expected_pages),
                                        "native_rows": len(native_rows)})
            audit.check("dd-247 native pages semantically parsed", all(p["successful"] for p in pages))
    return summary


def run_audit(args):
    audit = Audit(args.archive.resolve(), args.output.resolve())
    default_archive = audit.archive
    source_archives = {source: Path(path).resolve() for source, path in
                       (value.split("=", 1) for value in args.source_archive)}
    if args.dns_json:
        mapping = json.loads(args.dns_json.read_text())
        for host, addresses in mapping.items():
            response = json.loads((args.dns_json.parent / "source-investigation" / f"{host}-dns.json").read_text())
            public_answers = {a["data"] for a in response.get("Answer", []) if a.get("type") == 1}
            audit.check(f"{host} temporary DNS override is public and source backed",
                        bool(addresses) and set(addresses) <= public_answers and
                        all(ipaddress.ip_address(address).is_global for address in addresses), addresses)
    with psycopg.connect(os.environ["SEAL_DATABASE_URL"], row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        tables = catalog(conn)
        dump(audit.output / "catalog.json", tables)
        names = {"run": identify(tables, {"source_id", "config_hash", "report", "attempt_epoch"}),
                 "obs": identify(tables, {"run_id", "resource_id", "body_hash", "archive_ref", "attempt_epoch"}),
                 "version": identify(tables, {"crawl_result", "resource_id", "observation_id"})}
        runs = {str(r["id"]): r for r in table_rows(conn, names["run"])}
        all_obs = table_rows(conn, names["obs"])
        versions = {str(r["id"]): r for r in table_rows(conn, names["version"])}
        dump(audit.output / "versions.json", list(versions.values()))
        summaries, source_rows = [], {}
        for arg in args.receipt:
            source, path = arg.split("=", 1)
            audit.archive = source_archives.get(source, default_archive)
            receipt = json.loads(Path(path).read_text())
            run_id = str(receipt["run_id"])
            run = runs[run_id]
            audit.check(f"{source} receipt run Source", run["source_id"] == source)
            obs = [o for o in all_obs if str(o["run_id"]) == run_id]
            dump(audit.output / source / f"run-{run_id}.json", run)
            rows = inspect_inputs(audit, source, run, obs, versions)
            source_rows.setdefault(source, []).extend(rows)
        for source, rows in source_rows.items():
            audit.archive = source_archives.get(source, default_archive)
            latest = {}
            for row in sorted(rows, key=lambda row: str(row["observation"]["observed_at"])):
                latest[row["observation"]["url"]] = row
            before = len(audit.assertions)
            case = business(audit, source, list(latest.values()))
            if any(a["status"] == "FAIL" for a in audit.assertions[before:]):
                case["status"] = "FAIL"
            summaries.append(case)
    failures = [a for a in audit.assertions if a["status"] == "FAIL"]
    summary = {"status": "FAIL" if failures else "PASS", "assertions": len(audit.assertions),
               "failures": len(failures), "cases": summaries, "unverified": [
                   "Whole-site completeness", "Semantic accuracy of institution/school facts",
                   "Manual PDF visual/OCR fidelity", "Images and downloads beyond SDK-declared fields"]}
    dump(audit.output / "assertions.json", audit.assertions)
    dump(audit.output / "cas-hashes.json", audit.cas_evidence)
    dump(audit.output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--receipt", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dns-json", type=Path)
    parser.add_argument("--source-archive", action="append", default=[])
    args = parser.parse_args()
    return run_audit(args)


if __name__ == "__main__":
    raise SystemExit(main())
