"""Offline raw-byte oracle independent of SEAL's parser and PASS assertions."""

import argparse
import csv
import hashlib
import json
import sys
from io import StringIO
from pathlib import Path


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def object_bytes(root, key):
    value = (root / "archive/objects" / key[:2] / key[2:]).read_bytes()
    assert hashlib.sha256(value).hexdigest() == key, "archive_digest_mismatch"
    return value


def object_json(root, key):
    return json.loads(object_bytes(root, key))


def verify(root):
    manifest = json.loads((root / "manifest.json").read_text())
    collection = manifest.get("collection_sha256", {})
    repository = Path(__file__).resolve().parents[2]
    for path, expected in collection.items():
        assert manifest["code"][path] == expected, "collection_manifest_digest_mismatch"
        assert hashlib.sha256((repository / path).read_bytes()).hexdigest() == expected, (
            "collection_code_drift"
        )
    for path, expected in manifest["files"].items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == expected, (
            "artifact_digest_mismatch"
        )
    fixtures = json.loads((root / "fixture-oracle.json").read_text())
    collected = json.loads((root / "all-formats.json").read_text())
    documents = collected["export"]["documents"]
    assert len(documents) == 15, "missing_format_documents"
    formats, nodes, raw_values, relations = set(), 0, 0, 0
    identities = {document["id"]: document for document in documents}
    for document in documents:
        content = {"format": document["source_format"], "children": document["children"]}
        assert digest(content) == document["content_hash"], "document_content_hash_mismatch"
        assert document["type"] == "document", "mixed_resource_models"
        snapshot = object_json(root, document["snapshot_id"])
        body = object_bytes(root, snapshot["body_hash"])
        path = "/" + snapshot["url"].split("/", 3)[3]
        assert hashlib.sha256(body).hexdigest() == fixtures[path]["body_sha256"], (
            "raw_fixture_mismatch"
        )
        assert len(body) == snapshot["body_size"], "raw_size_mismatch"
        formats.add(document["source_format"])
        article_count = 0
        for node in document["children"]:
            nodes += 1
            assert node["type"] in {"article", "image", "table", "json", "document_ref"}, (
                "non_contract_node"
            )
            if node["type"] == "article":
                article_count += 1
                assert node["format"] == "text", "article_encoding_mismatch"
            if node["type"] == "json":
                assert node["value"] == json.loads(body), "json_root_mismatch"
                raw_values += 1
            if document["source_format"] == "csv":
                assert node["rows"] == list(csv.reader(StringIO(body.decode()))), (
                    "csv_raw_projection_mismatch"
                )
                raw_values += 1
            if document["source_format"] == "txt":
                assert node["text"] == body.decode(), "text_raw_projection_mismatch"
                raw_values += 1
            if document["source_format"] == "pdf":
                assert (
                    node["text"] == "Synthetic PDF notice" and b"(Synthetic PDF notice)" in body
                ), "pdf_text_layer_mismatch"
                raw_values += 1
            if document["source_format"] == "image":
                assert node["body_hash"] == snapshot["body_hash"], "image_input_reference_mismatch"
                raw_values += 1
            if node["type"] == "document_ref":
                relations += 1
                target = identities[node["document_id"]]
                assert target["url"] == node["url"], "reference_foreign_key_mismatch"
        assert article_count <= 1, "paragraph_level_article_split"
    assert formats == {"html", "pdf", "docx", "xls", "xlsx", "csv", "txt", "json", "image"}, (
        "declared_format_missing"
    )
    record = json.loads((root / "business-record.json").read_text())["records"][0]
    assert record["data"] == {"issued_year": "2026", "issuing_authority": None}, (
        "business_mapping_mismatch"
    )
    evidence = record["field_evidence"]["issued_year"]
    primary = object_json(root, evidence["snapshot_id"])
    original = json.loads(object_bytes(root, primary["body_hash"]))
    assert evidence["raw_value"] == original["year"] == 2026, "business_original_value_mismatch"
    assert set(record["field_evidence"]) == {"issued_year"}, "null_field_evidence_added"
    assert record["document_versions"][0]["snapshot_id"] == evidence["snapshot_id"], (
        "record_document_input_mismatch"
    )
    cross = json.loads((root / "cross-resource-business-record.json").read_text())
    record = cross["export"]["records"][0]
    refs = record["document_versions"]
    assert len(refs) == 2 and len({ref["snapshot_id"] for ref in refs}) == 2, (
        "cross_resource_inputs_missing"
    )
    assert {ref["snapshot_id"] for ref in refs} == set(record["snapshot_ids"]), (
        "cross_resource_raw_lineage_mismatch"
    )
    evidence = record["field_evidence"]["issued_year"]
    secondary = object_json(root, evidence["snapshot_id"])
    assert (
        json.loads(object_bytes(root, secondary["body_hash"]))["year"]
        == evidence["raw_value"]
        == 2027
    ), "cross_resource_field_mismatch"
    assert record["data"]["issued_year"] == "2027", "cross_resource_transform_mismatch"
    replays = 0
    for path in sorted(root.glob("*replay.json")):
        value = json.loads(path.read_text())
        receipt = value.get("receipt", value)
        assert receipt["mode"] == "replay", "wrong_replay_receipt"
        assert receipt["summary"]["counts"]["http_attempts"] == 0, "replay_network_attempted"
        replays += 1
    assert replays >= 4, "replay_paths_not_exercised"
    return {
        "status": "PASS",
        "files": len(manifest["files"]),
        "collection_code_files": len(collection),
        "formats": len(formats),
        "documents": len(documents),
        "nodes": nodes,
        "raw_value_checks": raw_values,
        "foreign_keys": relations,
        "cross_resource_inputs": len(refs),
        "replays": replays,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        value = verify(args.output)
    except Exception as exc:
        value = {"status": "FAIL", "reason": str(exc) or type(exc).__name__}
    print(json.dumps(value))
    return int(value["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
