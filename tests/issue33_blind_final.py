"""Assert API-only acceptance artifacts and emit a concise, reproducible result.

Preconditions: run prepare, controlled --fresh, real --fresh and the scope scenario in the
same checkout. Caller-provided storage/OCR settings must identify the same test environment.
"""

import hashlib
import json

from issue33_blind_acceptance import (
    ART,
    ROOT,
    load_env,
    object_bytes,
    query,
    reproduction_command,
    setup,
)
from issue33_blind_api import COLUMNS, source_hashes


def main():
    controlled = json.loads((ART / "controlled/assertions.json").read_text())
    scope = json.loads((ART / "scope/assertions.json").read_text())
    assert len(controlled) >= 19 and all(item["passed"] for item in controlled)
    assert scope["status"] == "PASS" and len(scope["checks"]) == 4
    reports, acquisition_checks = {}, {}
    for phase in ["controlled", "real"]:
        report = json.loads((ART / phase / "api-reconciliation.json").read_text())
        assert report["contract"] == "API-only"
        assert tuple(report["business_columns"]) == COLUMNS
        assert report["API_CAS_SHA256_equal"] and report["SQL_API_all_16_fields_equal"]
        assert report["api_rows"] == (6 if phase == "controlled" else 5)
        assert all(item["resolved"] for item in report["lineage_checks"])
        assert report["full_history_coverage"] == "UNKNOWN"
        expected = (
            {"dd-492": 2, "dd-499": 2} if phase == "controlled" else {"dd-492": 3, "dd-499": 2}
        )
        for source, count in expected.items():
            summary = report["source_summary"][source]
            assert summary["current_consumable_documents"] == count
            assert summary["quality_passed_documents"] == count
            assert summary["full_history_denominator"] == "UNKNOWN"
        env = setup(phase)
        acquired = query(
            env, "SELECT archive_ref FROM seal.acquisition WHERE archive_ref IS NOT NULL"
        )
        for item in acquired:
            object_bytes(env, item["archive_ref"])
        acquisition_checks[phase] = len(acquired)
        reports[phase] = report
    real_sql = json.loads((ART / "real/after-collect-sql.json").read_text())
    assert sum(item["http_method"] == "POST" for item in real_sql["acquisitions"]) >= 1
    assert len(real_sql["resources"]) == len(real_sql["versions"]) == 5
    ocr_documents = []
    for ref in reports["real"]["references"]:
        if ref["kind"] != "result_ref":
            continue
        doc = json.loads((ART / "real/cas" / (ref["sha256"] + ".json")).read_bytes())
        assert len(doc["content"]["markdown"]) > 20
        for item in doc["extraction"]["deliverables"]:
            if item.get("method") == "ocr":
                assert item["ocr"]["engine"] == "tesseract" and item["ocr"]["languages"] == "vie"
                ocr_documents.append(
                    {
                        "external_key": doc["identity"]["external_key"],
                        "pages": item["analysis"]["page_count"],
                    }
                )
    assert ocr_documents and max(item["pages"] for item in ocr_documents) >= 9
    assert "page_limit_exceeded" in json.dumps(reports["real"]["snapshot_coverage"])
    api = json.loads((ART / "controlled/api-results.json").read_text())
    rows = api["dd-499"]["items"]
    assert len(rows) == 2 and all(not row["原始文件"] and not row["来源详情页"] for row in rows)
    assert all(row["original_hash"] is None and len(row["markdown_hash"]) == 64 for row in rows)
    start = json.loads((ART / "frozen-source-hashes-start.json").read_text())
    end = source_hashes()
    (ART / "frozen-source-hashes-end.json").write_text(json.dumps(end, indent=2))
    assert start == end, "source_changed_during_acceptance"
    env = load_env()
    secrets = [
        value.encode()
        for key, value in env.items()
        if key in ["SEAL_DATABASE_URL", "SEAL_S3_ACCESS_KEY", "SEAL_S3_SECRET_KEY", "PGPW"]
        and len(value) >= 8
    ]
    files = [path for path in ART.rglob("*") if path.is_file()]
    assert not any(secret in path.read_bytes() for path in files for secret in secrets)
    commands = [
        reproduction_command("issue33_blind_acceptance.py", "prepare"),
        reproduction_command("issue33_blind_acceptance.py", "controlled", "--fresh"),
        reproduction_command("issue33_blind_acceptance.py", "real", "--fresh"),
        reproduction_command("issue33_blind_scope.py"),
        reproduction_command("issue33_blind_final.py"),
    ]
    result = {
        "status": "PASS",
        "contract": "API-only",
        "controlled_assertions": len(controlled),
        "scope_scenarios": len(scope["checks"]),
        "real_documents": 5,
        "real_api_rows": 5,
        "controlled_api_rows": 6,
        "real_ocr_documents": ocr_documents,
        "all_acquisition_CAS_hash_checks": acquisition_checks,
        "source_tree_sha256": hashlib.sha256(json.dumps(end, sort_keys=True).encode()).hexdigest(),
        "source_unchanged_during_acceptance": True,
        "test_hashes": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "tests").glob("issue33_blind_*"))
            if path.is_file()
        },
        "artifact_secret_value_hits": 0,
        "commands": commands,
        "real_collection_receipts": {
            source: json.loads((ART / "real" / (source + "-collect.json")).read_text())["result"]
            for source in ["dd-492", "dd-499"]
        },
        "scope": "Real Vietnam 3 documents and Federal Register 2 full-text rules; full-history denominator UNKNOWN. Multiple complete documents in one shared response use a controlled source.",
        "prerequisites": [
            "caller-provided SEAL_DATABASE_URL and S3 credentials with isolated CREATE DATABASE/CreateBucket rights",
            "project dependencies installed in the invoking Python interpreter",
            "Tesseract and caller-provided TESSDATA_PREFIX when needed; vie/eng models",
            "optional SEAL_TEST_ENV_FILE explicitly selects a POSIX shell environment file",
            "public source reachability; loopback permitted only for controlled fixtures",
        ],
        "limitations": [
            "No full-history coverage, statutory correctness or OCR character-accuracy claim.",
            "No live br-ocr-v1 remote service exercised.",
        ],
    }
    (ART / "final-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "real_collection_receipts"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
