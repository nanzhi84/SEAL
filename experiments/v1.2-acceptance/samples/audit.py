"""Recompute the V1.2 sample inventory from immutable research artifacts, offline.

This is evidence auditing, not a Runtime run or content quality evaluation.
No missing response is reconstructed from historical assertions.
"""

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
IDS = (
    "dd-017",
    "dd-041",
    "dd-116",
    "dd-102",
    "dd-468",
    "dd-004",
    "dd-247",
    "dd-009",
    "dd-248",
    "dd-250",
)
MECHANISMS = {
    "dd-017": ["html_list_detail", "numbered_pagination"],
    "dd-041": ["html_list_detail", "js_declared_pagination"],
    "dd-116": ["html_list_detail", "js_declared_pagination"],
    "dd-102": ["static_iframe", "html_table", "fixed_siteid", "numbered_pagination"],
    "dd-468": ["html_document", "static_xls_attachment"],
    "dd-004": ["html_announcement_list", "static_pdf_attachment", "gb18030"],
    "dd-247": ["multi_record_json_api", "fixed_blank_filters", "numbered_pagination"],
    "dd-009": ["fixed_public_query", "html_result_list", "numbered_pagination"],
    "dd-248": ["multi_record_json_api", "fixed_blank_filters", "numbered_pagination"],
    "dd-250": ["multi_record_json_api", "numbered_pagination"],
}
FIELDS = {
    "dd-247": ["houseName", "registerAddr", "officeAddr", "website", "phone"],
    "dd-248": ["orgName", "regAddr", "orgType", "checkTime"],
    "dd-250": ["trustName", "regAddr"],
}
MISSING_INPUT = {
    "dd-017": "required_detail_response_missing",
    "dd-041": "required_detail_response_missing",
    "dd-116": "required_detail_response_missing",
    "dd-102": "required_iframe_business_response_missing",
    "dd-468": "required_xls_attachment_response_missing",
    "dd-004": "required_pdf_attachment_response_missing",
    "dd-247": "required_json_business_response_missing",
    "dd-009": "required_fixed_query_result_response_missing",
    "dd-248": "required_json_business_response_missing",
    "dd-250": "required_json_business_response_missing",
}
CLASS_MECHANISMS = {
    "A": {
        "proposed_recipe_families": [
            "v12-reviewed-source-samples",
            "heterogeneous-public-records",
        ],
        "enumerability": "Static article/list/table channels can enumerate observed pages; classification does not establish whole-site or historical completeness",
        "pagination": "HTML anchors or an inspected static JS page naming rule; native Scrapy deduplication plus finite Source/sample limits",
        "attachments": "Explicit business PDF/document links only; static same-host iframe is an input resource, not a rendered browser session",
        "identity": "Detail URL for documents; stable business field for multi-row tables, never row position",
        "selected_evidence_ids": ["dd-017", "dd-041", "dd-116", "dd-102"],
    },
    "B": {
        "proposed_recipe_families": [
            "v12-reviewed-source-samples",
            "heterogeneous-public-records",
        ],
        "enumerability": "Candidate HTML/attachment links are enumerable on sampled pages; linked business bodies still require verification",
        "pagination": "Only inspected explicit links or source-specific static rules; a readable entry is not pagination acceptance",
        "attachments": "Archive explicit static PDF/XLS/DOC business links; parse supported text PDF/TXT/CSV and retain unsupported Excel/OCR failures",
        "identity": "Business document or attachment URL, with parent discovery/input lineage",
        "selected_evidence_ids": ["dd-468", "dd-004"],
    },
    "C": {
        "proposed_recipe_families": [
            "heterogeneous-public-records",
            "source-specific-public-query",
        ],
        "enumerability": "Frozen public query/API parameters enumerate a bounded result set; no claims about precise entity matching or complete register coverage",
        "pagination": "Source-specific pageNo/pageSize JSON envelope or explicit HTML query page links; page reordering does not change Record identity",
        "attachments": "None required by the inspected C samples; no unobserved attachment endpoints are invented",
        "identity": "Stable API business key must be verified from response bytes; HTML query results may use public company IDs/detail URLs",
        "selected_evidence_ids": ["dd-247", "dd-009", "dd-248", "dd-250"],
    },
}


def body_evidence(relative, expected):
    path = ROOT / relative
    result = {"path": relative, "exists": path.is_file(), "expected_sha256": expected}
    if path.is_file():
        body = path.read_bytes()
        result.update(
            bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            hash_verified=hashlib.sha256(body).hexdigest() == expected,
        )
    return result


def main():
    classification = ROOT / "experiments/source-accessibility/results/classification/entries.csv"
    with classification.open(encoding="utf-8-sig", newline="") as stream:
        rows = {row["id"]: row for row in csv.DictReader(stream)}
    classification_counts = dict(sorted(Counter(row["class"] for row in rows.values()).items()))
    abc = [row for row in rows.values() if row["class"] in {"A", "B", "C"}]
    class_summaries = {}
    for category in "ABC":
        members = [row for row in abc if row["class"] == category]
        class_summaries[category] = {
            "count": len(members),
            "research_ids": [row["id"] for row in members],
            "priority_counts": dict(sorted(Counter(row["priority"] for row in members).items())),
            **CLASS_MECHANISMS[category],
            "scope": "Machine-derived mechanism grouping, not an adaptation/audit status ledger or bulk adaptation acceptance",
        }
    old = ROOT / "experiments/due-diligence/results/map-20261009-resume/results.jsonl"
    prior = {row["id"]: row for row in map(json.loads, old.read_text().splitlines())}
    fixtures = json.loads(
        (ROOT / "experiments/due-diligence/results/freeze-20261009-v1/fixtures.json").read_text()
    )
    encoding = {row["id"]: row.get("encoding") for row in fixtures}
    samples = []
    for identity in IDS:
        row = rows[identity]
        raw = row["body_evidence"]
        sample = {
            "research_id": identity,
            "class": row["class"],
            "name": row["name"],
            "entry_url": row["entry_url"],
            "sample_url": row["sample_url"],
            "candidate_url": row["candidate_url"],
            "mechanisms": MECHANISMS[identity],
            "parameters": row["parameters"],
            "business_scope": row["scope"],
            "latest_research_body": body_evidence(raw, Path(raw).name),
            "runtime_acceptance": "BLOCKED",
            "source_id": None,
            "binding_id": None,
            "run_ids": [],
            "record_ids": [],
            "replay_run_ids": [],
            "blockers": [
                "restricted_network_destination",
                "required_live_runtime_rounds_missing",
                MISSING_INPUT[identity],
            ],
        }
        previous = prior.get(identity)
        if previous and previous.get("raw_path"):
            relative = str(old.parent.relative_to(ROOT) / previous["raw_path"])
            sample["historical_entry_body"] = body_evidence(relative, previous["body_sha256"])
            sample["historical_entry_body"].update(
                url=previous["url"],
                fetched_at=previous["fetched_at"],
                encoding=encoding.get(identity),
                scope="Historical entry response only; does not establish V1.2 collection acceptance",
            )
            if (
                sample["historical_entry_body"].get("hash_verified")
                and previous["body_sha256"] == Path(raw).name
            ):
                sample["latest_research_body"]["equivalent_retained_bytes"] = relative
                sample["latest_research_body"]["equivalent_hash_verified"] = True
        if identity in FIELDS:
            sample["json_contract_from_real_html"] = {
                "records_pointer": "/data/data/dataList",
                "total_pointer": "/data/data/total",
                "displayed_fields": FIELDS[identity],
                "detail_url_rendered": False,
                "stable_business_key": "UNVERIFIED; JSON response bytes are missing",
                "business_response_route_comparison": "BLOCKED",
            }
        samples.append(sample)
    output = {
        "artifact_version": 1,
        "scope": "Read-only evidence and source mechanism audit; not production Evaluation",
        "base_commit": "ee39884",
        "classification_input_sha256": hashlib.sha256(classification.read_bytes()).hexdigest(),
        "registry_summary": {
            "registry_rows": len(rows),
            "registry_classification_counts": classification_counts,
            "abc_rows": len(abc),
            "abc_classification_counts": {
                category: class_summaries[category]["count"] for category in "ABC"
            },
            "abc_priority_counts": dict(sorted(Counter(row["priority"] for row in abc).items())),
            "baseline_partition_verified": len(rows) == 491
            and len(abc) == 111
            and {category: class_summaries[category]["count"] for category in "ABC"}
            == {"A": 34, "B": 73, "C": 4},
            "classification_source": str(classification.relative_to(ROOT)),
            "priority_and_manual_state_authority": "docs/reference/source-adaptation.md#9-source-适配与审计总表",
            "priority_basis": "P0/P1/P2 values are read from the frozen classification CSV and refer to the sole maintained source table; no new human adaptation/audit statuses are maintained here",
        },
        "class_mechanism_summary": class_summaries,
        "selected_primary": {
            "A": ["dd-116", "dd-102"],
            "B": ["dd-468", "dd-004"],
            "C": ["dd-247", "dd-009"],
        },
        "substitution_review": {
            "C": {
                "candidates": ["dd-248", "dd-250"],
                "decision": "BLOCKED",
                "reason": "Same AMAC host is outside network policy; JSON bytes are also missing",
            },
            "A_B": {
                "decision": "No substitute claimed",
                "reason": "Missing details/iframe contents/attachments and common network boundary cannot be repaired by relabelling historical entry HTML",
            },
        },
        "shared_requirements": [
            "Bound source by business column/path, not whole domain",
            "Separate fixed business parameters, pagination controls, and user inputs",
            "Record identity must not be HTTP page URL or array offset",
            "Use Scrapy requests for lists, details, iframes and business attachments",
            "Replay must recompute typed locators from retained response bytes",
            "Page limit or missing response keeps business coverage unknown",
            "quality_status remains not_evaluated",
        ],
        "T1_mechanism_audit": "PASS",
        "T0_real_C_route_comparison": "BLOCKED",
        "T7S_two_real_sources_per_class": "BLOCKED",
        "two_live_collection_rounds": "BLOCKED",
        "valid_new_runtime_sources_per_class": {"A": 0, "B": 0, "C": 0},
        "samples": samples,
    }
    (HERE / "audit.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: output[key]
                for key in (
                    "T1_mechanism_audit",
                    "T0_real_C_route_comparison",
                    "T7S_two_real_sources_per_class",
                    "valid_new_runtime_sources_per_class",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
