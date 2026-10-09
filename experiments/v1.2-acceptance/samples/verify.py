"""Offline source adapter checks over retained real HTML, with network forbidden.

The snapshots below are isolated local representations of historical HTTP
responses. They are not persisted Runtime observations, runs or live samples.
"""

import importlib.util
import json
import os
import socket
import tempfile
from pathlib import Path

import jsonschema
from scrapy import Request
from scrapy.http import HtmlResponse

from seal.config import SourceConfig, load_file
from seal.core import Objects, digest
from seal.records import validate_record

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def no_network(*args, **kwargs):
    raise RuntimeError("offline_network_forbidden")


def main():
    socket.create_connection = no_network
    socket.socket.connect = no_network
    socket.getaddrinfo = no_network
    spec = importlib.util.spec_from_file_location(
        "v12_samples_offline", ROOT / "recipes/samples_v12/recipe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = load_file(ROOT / "recipes/samples_v12/recipe.yaml")
    audit = json.loads((HERE / "audit.json").read_text())
    rows = {row["research_id"]: row for row in audit["samples"]}
    checks, projections = [], []

    def check(name, actual, expected):
        checks.append(
            {
                "name": name,
                "actual": actual,
                "expected": expected,
                "status": "PASS" if actual == expected else "FAIL",
            }
        )

    previous_archive = os.environ.get("SEAL_ARCHIVE")
    (ROOT / "work/v1.2-samples").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "work/v1.2-samples") as temp:
        os.environ["SEAL_ARCHIVE"] = temp
        try:
            for identity in ("dd-116", "dd-468", "dd-004", "dd-102"):
                source = json.loads((HERE / f"{identity}-source.json").read_text())
                SourceConfig.model_validate(source)
                params = json.loads((HERE / f"{identity}-params.json").read_text())
                jsonschema.validate(params, manifest["params_schema"])
                evidence = rows[identity]["historical_entry_body"]
                raw = (ROOT / evidence["path"]).read_bytes()
                check(identity + ":historical_hash", digest(raw), evidence["expected_sha256"])
                objects = Objects()
                snapshot = objects.put_json(
                    {
                        "url": evidence["url"],
                        "method": "GET",
                        "status": 200,
                        "body_hash": objects.put(raw),
                        "encoding": evidence["encoding"],
                    }
                )
                request = Request(
                    evidence["url"],
                    meta={
                        "seal_role": source["seed_role"],
                        "seal_snapshot_id": snapshot,
                        "seal_observation_id": "offline-test-only",
                    },
                )
                response = HtmlResponse(
                    evidence["url"],
                    body=raw,
                    encoding=evidence["encoding"],
                    request=request,
                    headers={"Content-Type": "text/html"},
                )
                spider = module.SampleSpider(params, {"config": source, "seeds": []})
                output = list(spider.parse(response))
                requests = [item for item in output if isinstance(item, Request)]
                records = [
                    item for item in output if isinstance(item, dict) and item["type"] == "record"
                ]
                check(
                    identity + ":no_response_ids_copied_to_requests",
                    all(
                        "seal_snapshot_id" not in item.meta
                        and "seal_observation_id" not in item.meta
                        for item in requests
                    ),
                    True,
                )
                check(
                    identity + ":all_discoveries_keep_parent",
                    all(item.meta["seal_parent_snapshot_id"] == snapshot for item in requests),
                    True,
                )
                for item in records:
                    normalized, lineage = validate_record(item)
                    check(identity + ":locators_and_identity_recomputed", len(lineage), 1)
                    # Do not publish the local test-only observation/snapshot as
                    # a Source/Run/Record identity or new Runtime archive.
                    projections.append(
                        {
                            "research_id": identity,
                            "raw_body": evidence["path"],
                            "raw_sha256": digest(raw),
                            "data": normalized["data"],
                            "record_key": normalized["record_key"],
                            "locators": normalized["locators"],
                            "quality_status": "not_evaluated",
                            "scope": "Offline recipe projection of historical raw HTML; not a new Runtime result",
                        }
                    )
                if identity == "dd-116":
                    detail = [item for item in requests if item.meta["seal_role"] == "detail"]
                    pages = [item for item in requests if item.meta["seal_role"] == "list"]
                    check(identity + ":twenty_real_discoveries", len(detail), 20)
                    check(
                        identity + ":two_selected_details",
                        sum(not item.meta.get("seal_helper_rejection") for item in detail),
                        2,
                    )
                    check(
                        identity + ":eighteen_explicit_scope_limits",
                        sum(
                            item.meta.get("seal_helper_rejection") == "sample_detail_limit"
                            for item in detail
                        ),
                        18,
                    )
                    check(
                        identity + ":first_real_detail",
                        detail[0].url,
                        "http://amr.haikou.gov.cn/xxgk/spgsxx/xzcfangsxx/202610/t1548476.shtml",
                    )
                    check(
                        identity + ":declared_next_page",
                        [item.url for item in pages],
                        ["http://amr.haikou.gov.cn/xxgk/spgsxx/xzcfangsxx/index_1.shtml"],
                    )
                elif identity == "dd-468":
                    check(identity + ":one_structured_notice", len(records), 1)
                    check(
                        identity + ":real_notice_title",
                        records[0]["data"]["title"],
                        "全国高等学校名单",
                    )
                    check(
                        identity + ":real_notice_fact",
                        "全国高等学校共计3012所" in records[0]["data"]["body"],
                        True,
                    )
                    check(
                        identity + ":two_real_xls_urls",
                        [item.url.rsplit("/", 1)[-1] for item in requests],
                        [
                            "W020211027623974108131.xls",
                            "W020211027623974133601.xls",
                        ],
                    )
                    check(
                        identity + ":attachment_parent_input",
                        all(
                            item.cb_kwargs["parent"]["snapshot_id"] == snapshot for item in requests
                        ),
                        True,
                    )
                elif identity == "dd-004":
                    check(identity + ":gb18030_real_pdf_links", len(requests), 4)
                    check(
                        identity + ":two_selected_attachments",
                        sum(not item.meta.get("seal_helper_rejection") for item in requests),
                        2,
                    )
                    check(identity + ":no_fabricated_attachment_record", records, [])
                elif identity == "dd-102":
                    check(
                        identity + ":one_static_iframe",
                        [item.url for item in requests],
                        ["https://www.safe.gov.cn/www/illegal?siteid=beijing"],
                    )
                    check(identity + ":no_fabricated_iframe_record", records, [])
        finally:
            if previous_archive is None:
                os.environ.pop("SEAL_ARCHIVE", None)
            else:
                os.environ["SEAL_ARCHIVE"] = previous_archive
    report = {
        "artifact_version": 1,
        "network": "forbidden",
        "scope": "Historical real HTML adapter and locator checks, separate from live Runtime acceptance",
        "assertions": len(checks),
        "passed": sum(check["status"] == "PASS" for check in checks),
        "failed": sum(check["status"] == "FAIL" for check in checks),
        "checks": checks,
        "live_runtime_acceptance": "BLOCKED",
    }
    (HERE / "offline-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    (HERE / "offline-projections.json").write_text(
        json.dumps(projections, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("assertions", "passed", "failed", "live_runtime_acceptance")
            },
            ensure_ascii=False,
        )
    )
    return bool(report["failed"])


if __name__ == "__main__":
    raise SystemExit(main())
