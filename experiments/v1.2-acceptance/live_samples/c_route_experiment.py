"""Compare legacy URL identity with Record identity on frozen public API bytes.

Run only inside the isolated acceptance PostgreSQL after migrations. Temporary
tables copy the project's actual constraints and disappear with the connection;
this comparison writes no persistent Source/Run/Document/Record rows.
"""

import argparse
import json
from pathlib import Path

import psycopg

from seal.core import digest, uid
from seal.db import connect, j

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FIELDS = ("houseName", "registerAddr", "officeAddr", "website", "phone")
API = (
    "https://www.amac.org.cn/portal/front/mutualFund/findMutualFundHousePage"
    "?pageNo=1&pageSize=10&houseName=&registerAddr=&officeAddr="
)


def comparison(output):
    inputs = {}
    for name in ("dd-247-page1", "dd-247-page2", "dd-247-filter", "dd-247-pagesize20"):
        path = HERE / "c-evidence" / (name + ".json")
        body = path.read_bytes()
        envelope = json.loads(body)
        assert envelope["code"] == 200 and envelope["data"]["errcode"] == 0
        rows = envelope["data"]["data"]["dataList"]
        inputs[name] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": digest(body),
            "rows": rows,
            "total": envelope["data"]["data"]["total"],
        }
    rows = inputs["dd-247-page1"]["rows"]
    all_rows = rows + inputs["dd-247-page2"]["rows"]
    names = [row["houseName"] for row in all_rows]
    filtered = inputs["dd-247-filter"]["rows"]
    twenty = inputs["dd-247-pagesize20"]["rows"]
    assert len(rows) == 10 and len(all_rows) == 20
    assert len(set(names)) == 20 and all(isinstance(name, str) and name for name in names)
    target = next(row for row in all_rows if row["houseName"] == "易方达基金管理有限公司")
    assert filtered == [target]
    assert {row["houseName"]: {field: row[field] for field in FIELDS} for row in twenty} == {
        row["houseName"]: {field: row[field] for field in FIELDS} for row in all_rows
    }
    with connect() as connection:
        connection.execute(
            "CREATE TEMP TABLE route_document "
            "(LIKE seal_document INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES)"
        )
        connection.execute(
            "CREATE TEMP TABLE route_record "
            "(LIKE seal_record INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES)"
        )
        for row in rows:
            # This is the legacy items.stage mapping: identity and url both
            # come from the archived Candidate URL, never from row position.
            connection.execute(
                "INSERT INTO route_document(id,source_id,namespace,identity,url) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (uid(), "route-only", "test", API, API),
            )
            connection.execute(
                "INSERT INTO route_record"
                "(id,source_id,namespace,record_type,record_key,detail_url,parent_request) "
                "VALUES(%s,%s,%s,%s,%s,NULL,%s)",
                (
                    uid(),
                    "route-only",
                    "test",
                    "mutual_fund_manager",
                    row["houseName"],
                    j({"url": API, "role": "api", "method": "GET"}),
                ),
            )
        old_count = connection.execute("SELECT count(*) n FROM route_document").fetchone()["n"]
        new_count = connection.execute("SELECT count(*) n FROM route_record").fetchone()["n"]
        try:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO route_document(id,source_id,namespace,identity,url) "
                    "VALUES(%s,'route-only','test','null-url',NULL)",
                    (uid(),),
                )
        except psycopg.errors.NotNullViolation:
            null_url_rejected = True
        else:
            null_url_rejected = False
        assert old_count == 1 and new_count == 10 and null_url_rejected
    result = {
        "research_id": "dd-247",
        "experiment": "real_public_api_bytes_actual_postgresql_identity_constraints",
        "scope": "Compatibility route experiment only; real Runtime acceptance is separate.",
        "inputs": {
            name: {key: value for key, value in source.items() if key != "rows"}
            for name, source in inputs.items()
        },
        "schema": {
            "legacy": {
                "path": "src/seal/schema.sql",
                "sha256": digest((ROOT / "src/seal/schema.sql").read_bytes()),
            },
            "record": {
                "path": "src/seal/migrations/0003_records.sql",
                "sha256": digest((ROOT / "src/seal/migrations/0003_records.sql").read_bytes()),
            },
        },
        "legacy_document": {
            "attempted_business_rows": 10,
            "stored_url_identities": old_count,
            "null_url_rejected": null_url_rejected,
            "consequence": "Ten API rows share one actual archived URL. Legacy URL identity collapses them; null URL violates schema. Distinct invented URLs violate the Candidate archived-response URL contract.",
        },
        "record": {
            "attempted_business_rows": 10,
            "stored_business_identities": new_count,
            "detail_url": None,
            "frozen_parent_request": {"url": API, "role": "api", "method": "GET"},
            "key_field": "houseName",
            "unique_keys_across_two_pages": len(set(names)),
            "source_total_reported": inputs["dd-247-page1"]["total"],
        },
        "identity_evidence": {
            "natural_key": "Exact source-rendered legal institution name houseName",
            "cross_query_anchor": target["houseName"],
            "position_before": "pageNo=2&pageSize=10, array index 0",
            "position_after": "pageNo=1&pageSize=10&houseName=易方达, array index 0",
            "page_size_crosscheck": "pageSize=20 returns the same 20 legal names and displayed fields",
            "lineId_excluded": "Observed unchanged but official permanent ID semantics unverified; never used as record key or business data.",
            "limitation": "Renaming creates a new natural identity and the previous identity remains unknown; permanent registry-ID continuity is not claimed.",
        },
        "assertions": {
            "passed": 9,
            "failed": 0,
            "checks": [
                "page1_success_envelope",
                "page2_success_envelope",
                "filter_success_envelope",
                "pagesize20_success_envelope",
                "two_page_counts",
                "legal_names_unique",
                "fixed_name_filter_same_business_row",
                "page_size_same_business_fields",
                "legacy_collapses_record_preserves_and_null_url_rejected",
            ],
        },
        "persistent_rows_written": 0,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"route_experiment": "PASS", "legacy": old_count, "record": new_count}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=HERE / "c-route-experiment.json")
    comparison(parser.parse_args().output)
