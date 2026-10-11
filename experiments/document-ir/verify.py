"""Independently recompute archived body and exported document hashes."""

import argparse
import hashlib
import json
from pathlib import Path


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def archived(root, identity):
    body = (root / "archive/objects" / identity[:2] / identity[2:]).read_bytes()
    if sha(body) != identity:
        raise AssertionError("archive_hash_mismatch")
    return body


def verify(root):
    assertions = json.loads((root / "assertions.json").read_text())
    if not assertions or any(item["status"] != "PASS" for item in assertions):
        raise AssertionError("behavior_assertions_failed")
    checked, refs = 0, 0
    for filename in (
        "collection",
        "formats",
        "repeat",
        "rule-change",
        "post",
        "post-replay",
        "business",
        "mapped",
        "recheck",
        "bad-selector",
        "sitemap",
        "nav-list",
    ):
        case = json.loads((root / f"{filename}.json").read_text())
        documents = case["export"]["content_documents"]
        for doc in documents:
            snapshot = json.loads(archived(root, doc["snapshot_id"]))
            original = archived(root, snapshot["body_hash"])
            if (
                snapshot["url"] != doc["url"]
                or snapshot["method"] != doc["method"]
                or snapshot["status"] != 200
                or snapshot["body_size"] != len(original)
                or snapshot["body_hash"] != doc["body_hash"]
            ):
                raise AssertionError("document_source_mismatch")
            if (
                sha(canonical({"type": "document", "children": doc["children"]}))
                != doc["content_hash"]
            ):
                raise AssertionError("document_content_hash_mismatch")
            for ref in doc["refs"]:
                child = doc["children"][ref["ordinal"]]
                if (
                    child["type"] != "document_ref"
                    or child["document_id"] != ref["document_id"]
                    or child["url"] != ref["url"]
                ):
                    raise AssertionError("foreign_key_export_mismatch")
                refs += 1
            checked += 1
    business = json.loads((root / "business.json").read_text())["export"]
    versions = {doc["document_version_id"] for doc in business["content_documents"]}
    for record in business["records"]:
        if (
            not record["document_version_ids"]
            or not set(record["document_version_ids"]) <= versions
        ):
            raise AssertionError("business_document_version_missing")
    return {
        "status": "PASS",
        "documents_checked": checked,
        "refs_checked": refs,
        "behavior_assertions": len(assertions),
        "basis": "saved CLI exports and immutable archive objects",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    result = verify(args.artifact)
    (args.artifact / "independent-verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
