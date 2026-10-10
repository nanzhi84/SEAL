"""XLS/DOCX contracts in the existing CLI/Scrapy/PostgreSQL acceptance harness."""

import hashlib
import json

from live_verify import verify_export
from runtime_support import archived, by_key, record_source, run


def structured_attachments(h, version):
    binding = record_source(
        h,
        "structuredassets",
        version,
        paths=["/assets/structured-list"],
        params={"mode": "assets"},
    )
    first, inspected, exported = run(
        h, "structuredassets", binding, capture="structured-attachments-first"
    )
    h.check("xls_docx_complete", first["status"], "complete")
    h.check("xls_docx_two_document_records", len(exported["records"]), 2)
    records = by_key(exported)
    xls = records[h.site.url + "/assets/public.xls"]
    docx = records[h.site.url + "/assets/public.docx"]
    h.check("xls_document_identity_is_attachment_url", xls["record_key"], xls["detail_url"])
    h.check("docx_document_identity_is_attachment_url", docx["record_key"], docx["detail_url"])
    sheets = xls["data"]["worksheets"]
    h.check("xls_all_sheets_preserved", [sheet["name"] for sheet in sheets], ["公开高校", "说明"])
    h.check("xls_merged_range_preserved", sheets[0]["merged_cells"], [[0, 1, 0, 4]])
    h.check(
        "xls_unicode_typed_business_cells", sheets[0]["rows"][2], ["11001", "示例大学", 42.5, True]
    )
    h.check(
        "xls_empty_positions_and_effective_bounds",
        [len(sheets[0]["rows"]), len(sheets[0]["rows"][0])],
        [5, 4],
    )
    h.check(
        "docx_original_block_order",
        [block.get("kind", "paragraph") for block in docx["data"]["blocks"]],
        ["paragraph", "table", "paragraph"],
    )
    h.check(
        "docx_merged_topology_preserved",
        docx["data"]["blocks"][1]["rows"][0]["cells"][0]["span"],
        2,
    )
    h.check(
        "docx_full_business_text",
        docx["data"]["body"],
        "行业分类合成公开样本\n门类与代码\nA\t农业\nB\t采矿业\n表格之后的解释文字",
    )
    h.check(
        "structured_attachment_independent_raw_oracle",
        verify_export(h.root / "archive", exported),
        [],
    )
    for suffix, expected in (("xls", h.site.xls), ("docx", h.site.docx)):
        observation = next(
            row for row in inspected["observations"] if row["url"].endswith("." + suffix)
        )
        snapshot = json.loads(archived(h, observation["snapshot_id"]))
        h.check(
            "structured_" + suffix + "_exact_archived_bytes",
            hashlib.sha256(archived(h, snapshot["body_hash"])).hexdigest(),
            hashlib.sha256(expected).hexdigest(),
        )
    initial_versions = {key: record["record_version_id"] for key, record in records.items()}
    initial_hashes = {
        ref["body_hash"] for record in exported["records"] for ref in record["inputs"]
    }
    before = len(h.site.ledger)
    replay = h.cli("replay", first["run_id"])
    replay_export = h.cli("export", "structuredassets", "--run", replay["run_id"])
    h.check("structured_replay_zero_network", len(h.site.ledger), before)
    h.check("structured_replay_complete", replay["status"], "complete")
    h.check(
        "structured_replay_stable_identity_data",
        {key: record["data"] for key, record in by_key(replay_export).items()},
        {key: record["data"] for key, record in records.items()},
    )
    h.check(
        "structured_replay_independent_oracle", verify_export(h.root / "archive", replay_export), []
    )
    h.capture("structured-attachments-replay", {"receipt": replay, "export": replay_export})
    before = len(h.site.ledger)
    recheck, _, checked = run(
        h, "structuredassets", binding, recheck=True, capture="structured-attachments-recheck"
    )
    h.check("structured_recheck_complete", recheck["status"], "complete")
    h.check(
        "structured_recheck_only_attachment_urls",
        {row["path"] for row in h.site.ledger[before:]},
        {"/assets/public.xls", "/assets/public.docx"},
    )
    h.check(
        "structured_recheck_stable_versions",
        {key: record["record_version_id"] for key, record in by_key(checked).items()},
        initial_versions,
    )
    h.check("structured_recheck_independent_oracle", verify_export(h.root / "archive", checked), [])
    # A later damaged response cannot replace the successful content baseline.
    h.site.structured_broken = True
    try:
        failed, _, unavailable = run(
            h,
            "structuredassets",
            binding,
            recheck=True,
            ok=False,
            capture="structured-attachments-damaged",
        )
    finally:
        h.site.structured_broken = False
    h.check("damaged_structured_run_partial", failed["status"], "partial")
    h.check(
        "damaged_structured_errors_explicit",
        {"xls_parse_failed", "docx_parse_failed"}.issubset(set(failed["errors"])),
    )
    h.check("damaged_structured_no_current_run_records", unavailable["records"], [])
    historical = h.cli("export", "structuredassets", "--run", first["run_id"])
    h.check(
        "damaged_structured_keeps_historical_versions",
        {key: record["record_version_id"] for key, record in by_key(historical).items()},
        initial_versions,
    )
    for key in sorted(initial_hashes):
        h.check(
            "structured_history_immutable_" + key, hashlib.sha256(archived(h, key)).hexdigest(), key
        )
    partial = record_source(
        h,
        "partialdocument",
        version,
        paths=["/assets/partial-list"],
        params={"mode": "assets", "allow_partial_documents": True},
    )
    failed, observed, retained = run(
        h, "partialdocument", partial, ok=False, capture="structured-attachments-partial"
    )
    h.check("docx_unsupported_object_partial", failed["status"], "partial")
    h.check("docx_unsupported_object_diagnostic", "docx_unsupported_content" in failed["errors"])
    h.check("docx_partial_supported_body_retained", len(retained["records"]), 1)
    h.check(
        "docx_partial_coverage_from_raw",
        retained["records"][0]["data"]["coverage"]["unparsed_elements"],
        {"altChunk": 1},
    )
    h.check("docx_partial_raw_oracle", verify_export(h.root / "archive", retained), [])
    h.check("docx_partial_original_archive_retained", len(observed["observations"]), 2)
