"""Recompute real business evidence offline, without importing SEAL resolvers."""

import argparse
import hashlib
import json
from datetime import date
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lxml import html
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
