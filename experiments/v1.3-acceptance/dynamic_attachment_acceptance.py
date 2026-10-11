"""Real CLI/PG/HTTP proof for extensionless attachment lineage and offline Replay.

The native request keeps its discovered role (detail) and fingerprint. Final
Content-Type chooses the attachment parser. Record provenance retains the first
actually downloaded parent; all duplicate references remain in Discovery.
XLSX is explicitly unsupported, with immutable downloaded bytes retained.
"""

import argparse
import hashlib
import importlib.util
import json
import sys
import threading
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
V12 = ROOT / "experiments/v1.2-acceptance"
sys.path.insert(0, str(V12))
SPEC = importlib.util.spec_from_file_location("seal_dynamic_acceptance", V12 / "acceptance.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
Harness = MODULE.Harness

from runtime_support import sql  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_BYTES = b"PK\x03\x04Synthetic unsupported XLSX representation"
TEXT_BYTES = b"Public dynamic text report\nThe original downloaded text must remain verifiable."
PARENT_BODY = "<h1>Public notice</h1><article><p>This is the explanatory parent page for a public legal report. Its archived representation is retained as supplementary provenance for each downloadable resource.</p></article>"


def body_object(h, snapshot):
    body_hash = snapshot["body_hash"]
    return (h.root / "archive/objects" / body_hash[:2] / body_hash[2:]).read_bytes()


def exercise(h):
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    # The production address guard always rejects the hostname localhost. Its
    # isolated-acceptance opt-in permits literal loopback IPs, so use a second
    # actual server instead of relaxing that guard for a fixture hostname.
    cross_server = ThreadingHTTPServer(("127.0.0.2", 0), handler)
    cross_url = "http://127.0.0.2:" + str(cross_server.server_address[1])
    cross_thread = threading.Thread(target=cross_server.serve_forever, daemon=True)
    cross_thread.start()

    def get(request):
        path = request.path
        content_type = "text/html; charset=utf-8"
        if path == "/robots.txt":
            content_type, body = "text/plain", b"User-agent: *\nAllow: /\n"
        elif path == "/dynamic/list":
            body = b'<html><nav><a href="/dynamic/notice">Notice</a><a href="/dynamic/also">Also</a></nav></html>'
        elif path == "/dynamic/notice":
            body = (
                "<html>"
                + PARENT_BODY
                + '<a href="/dynamic/download?id=pdf">PDF report</a><a href="/dynamic/download?id=txt">Text report</a></html>'
            ).encode()
        elif path == "/dynamic/also":
            body = (
                "<html>"
                + PARENT_BODY.replace("Public notice", "Second reference")
                + '<a href="/dynamic/download?id=pdf">Same public report</a></html>'
            ).encode()
        elif path == "/dynamic/xlsx-parent":
            body = (
                "<html>"
                + PARENT_BODY
                + '<a href="/dynamic/download?id=xlsx">Spreadsheet report</a></html>'
            ).encode()
        elif path == "/dynamic/cross-parent":
            body = (
                "<html>"
                + PARENT_BODY
                + '<a href="'
                + cross_url
                + '/dynamic/download?id=pdf">Cross-host report</a></html>'
            ).encode()
        elif path == "/dynamic/download?id=pdf":
            content_type, body = "application/pdf", h.site.pdf
        elif path == "/dynamic/download?id=txt":
            content_type, body = "text/plain; charset=utf-8", TEXT_BYTES
        elif path == "/dynamic/download?id=xlsx":
            content_type, body = XLSX_MIME, XLSX_BYTES
        else:
            return original(request)
        h.site.ledger.append(
            {"method": "GET", "path": path, "status": 200, "host": request.headers["Host"]}
        )
        request.send_response(200)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = get
    params = {"expand_homepage": False, "discover_sitemaps": False}
    try:
        version = h.cli("recipe", "pack", "recipes/seeded")["recipe_version"]
        h.config(
            "dynamic",
            entries=[h.site.url + "/dynamic/list"],
            output_schema="record.v1",
            allowed_path_prefixes=["/dynamic"],
            robots=True,
            delay=0.0,
            concurrency=1,
        )
        binding = h.binding("dynamic", version, params)
        run = h.cli("collect", "dynamic", "--binding", binding)
        inspection = h.cli("inspect", "run", run["run_id"])
        exported = h.cli("export", "dynamic", "--run", run["run_id"])
        h.capture("dynamic-collect", {"receipt": run, "inspect": inspection, "export": exported})
        records = {row["record_key"]: row for row in exported["records"]}
        events = inspection["discovery"]["events"]
        parent_event = next(
            e for e in events if e["url"] == h.site.url + "/dynamic/notice" and e["snapshot_id"]
        )
        notice_input = {
            "snapshot_id": parent_event["snapshot_id"],
            "observation_id": parent_event["observation_id"],
        }
        # Scrapy owns download order. A shared URL keeps the parent of its
        # actually accepted request, which can originate on either notice.
        accepted = {
            kind: next(
                event
                for event in events
                if event["url"] == h.site.url + "/dynamic/download?id=" + kind
                and event["snapshot_id"]
            )
            for kind in ("pdf", "txt")
        }
        parent_inputs = {
            kind: {
                "snapshot_id": event["parent_snapshot_id"],
                "observation_id": event["parent_observation_id"],
            }
            for kind, event in accepted.items()
        }
        h.check(
            "text_download_parent_is_its_only_referring_notice", parent_inputs["txt"], notice_input
        )
        h.check("dynamic_collect_complete", run["status"], "complete")
        h.check(
            "dynamic_final_type_counted_as_attachment", run["summary"]["counts"]["attachments"], 2
        )
        for identifier, expected_body, locator_kind in (
            ("pdf", "Synthetic PDF notice", "segments"),
            ("txt", TEXT_BYTES.decode(), "text"),
        ):
            url = h.site.url + "/dynamic/download?id=" + identifier
            record = records[url]
            parent_input = parent_inputs[identifier]
            h.check(
                identifier + "_classified_from_content_type",
                record["record_type"],
                "document_attachment",
            )
            h.check(identifier + "_source_bytes_projection", record["data"]["body"], expected_body)
            h.check(
                identifier + "_field_locator_kind", record["locators"]["body"]["kind"], locator_kind
            )
            h.check(
                identifier + "_has_explanatory_parent_input",
                parent_input
                in [{key: entry[key] for key in parent_input} for entry in record["inputs"]],
            )
            primary = record["primary_snapshot_id"]
            h.check(
                identifier + "_separate_main_and_parent_inputs",
                primary != parent_input["snapshot_id"],
            )
            result = sql(
                h,
                "SELECT candidate,checks,inputs FROM seal_record_result WHERE id=%s",
                (record["record_result_id"],),
            )[0]
            h.check(
                identifier + "_pipeline_verified_input_and_locators",
                result["checks"],
                {"input_verified": True, "locators_verified": True, "identity_verified": True},
            )
            h.check(
                identifier + "_verified_result_includes_parent_snapshot",
                parent_input["snapshot_id"] in result["inputs"],
            )
            h.check(
                identifier + "_frozen_discovery_role_preserved",
                all(e["role"] == "detail" for e in events if e["url"] == url),
            )
        pdf_events = [e for e in events if e["url"] == h.site.url + "/dynamic/download?id=pdf"]
        h.check("two_parent_references_remain_in_discovery", len(pdf_events), 2)
        h.check(
            "both_referring_notice_urls_remain_in_discovery",
            {e["parent_url"] for e in pdf_events},
            {h.site.url + "/dynamic/notice", h.site.url + "/dynamic/also"},
        )
        h.check(
            "all_duplicate_parent_references_have_snapshot_evidence",
            all(e["parent_snapshot_id"] and e["parent_observation_id"] for e in pdf_events),
        )
        h.check(
            "native_dupefilter_downloads_shared_pdf_once",
            sum(e["path"] == "/dynamic/download?id=pdf" for e in h.site.ledger),
            1,
        )
        h.check(
            "record_uses_first_accepted_parent",
            [
                {key: entry[key] for key in parent_inputs["pdf"]}
                for entry in records[h.site.url + "/dynamic/download?id=pdf"]["inputs"]
                if entry["snapshot_id"]
                != records[h.site.url + "/dynamic/download?id=pdf"]["primary_snapshot_id"]
            ],
            [parent_inputs["pdf"]],
        )
        before = len(h.site.ledger)
        replay = h.cli("replay", run["run_id"])
        replay_export = h.cli("export", "dynamic", "--run", replay["run_id"])
        h.capture("dynamic-replay", {"receipt": replay, "export": replay_export})
        h.check("dynamic_replay_complete", replay["status"], "complete")
        h.check(
            "dynamic_replay_zero_http_attempts", replay["summary"]["counts"]["http_attempts"], 0
        )
        h.check("dynamic_replay_zero_network", len(h.site.ledger), before)
        replay_records = {row["record_key"]: row for row in replay_export["records"]}
        for identifier in ("pdf", "txt"):
            url = h.site.url + "/dynamic/download?id=" + identifier
            h.check(
                identifier + "_replay_preserves_business_content",
                replay_records[url]["data"],
                records[url]["data"],
            )
            h.check(
                identifier + "_replay_preserves_original_parent_snapshot",
                parent_inputs[identifier]["snapshot_id"] in replay_records[url]["snapshot_ids"],
            )

        h.config(
            "dynamic_direct_recheck",
            entries=[h.site.url + "/dynamic/download?id=" + kind for kind in ("pdf", "txt")],
            output_schema="record.v1",
            robots=True,
            delay=0.0,
            allowed_path_prefixes=["/dynamic"],
        )
        direct_binding = h.binding("dynamic_direct_recheck", version, params)
        direct = h.cli("collect", "dynamic_direct_recheck", "--binding", direct_binding)
        before = len(h.site.ledger)
        recheck = h.cli(
            "collect", "dynamic_direct_recheck", "--binding", direct_binding, "--recheck"
        )
        direct_inspection = h.cli("inspect", "run", recheck["run_id"])
        direct_export = h.cli("export", "dynamic_direct_recheck", "--run", recheck["run_id"])
        h.capture(
            "dynamic-direct-recheck",
            {
                "collect": direct,
                "receipt": recheck,
                "inspect": direct_inspection,
                "export": direct_export,
            },
        )
        h.check("direct_attachment_recheck_complete", recheck["status"], "complete")
        h.check(
            "direct_attachment_recheck_matches_frozen_plan",
            direct_inspection["run"]["recheck_plan"]["unique_request_count"],
            2,
        )
        h.check(
            "direct_attachment_recheck_only_targets_and_policy",
            sorted(row["path"] for row in h.site.ledger[before:]),
            ["/dynamic/download?id=pdf", "/dynamic/download?id=txt", "/robots.txt"],
        )
        h.check("direct_attachment_recheck_emits_both_documents", len(direct_export["records"]), 2)
        h.check(
            "direct_attachment_recheck_no_parent_fetch_needed",
            all(len(row["inputs"]) == 1 for row in direct_export["records"]),
        )

        h.config(
            "dynamic_xlsx",
            entries=[h.site.url + "/dynamic/xlsx-parent"],
            output_schema="record.v1",
            robots=True,
            delay=0.0,
            allowed_path_prefixes=["/dynamic"],
        )
        xlsx_binding = h.binding("dynamic_xlsx", version, params)
        xlsx = h.cli("collect", "dynamic_xlsx", "--binding", xlsx_binding, ok=False)
        xlsx_inspect = h.cli("inspect", "run", xlsx["run_id"])
        xlsx_export = h.cli("export", "dynamic_xlsx", "--run", xlsx["run_id"])
        h.capture(
            "xlsx-unsupported", {"receipt": xlsx, "inspect": xlsx_inspect, "export": xlsx_export}
        )
        event = next(
            e for e in xlsx_inspect["discovery"]["events"] if e["url"].endswith("?id=xlsx")
        )
        snapshot = h.cli("inspect", "snapshot", event["snapshot_id"])
        h.check(
            "xlsx_has_explicit_unsupported_diagnostic",
            "unsupported_content_type" in xlsx["report"]["errors"],
        )
        h.check(
            "xlsx_no_false_attachment_record",
            any(r["record_type"] == "document_attachment" for r in xlsx_export["records"]),
            False,
        )
        h.check("xlsx_snapshot_stays_available", snapshot["archive"]["availability"], "available")
        h.check(
            "xlsx_original_representation_unchanged",
            hashlib.sha256(body_object(h, snapshot["snapshot"])).hexdigest(),
            hashlib.sha256(XLSX_BYTES).hexdigest(),
        )
        h.check("xlsx_original_mime_retained", XLSX_MIME in str(snapshot["snapshot"]["headers"]))
        for scoped in (False, True):
            name = "dynamic_cross_scoped" if scoped else "dynamic_cross_denied"
            extra = (
                {"host_path_scopes": {"127.0.0.1": ["/dynamic"], "127.0.0.2": ["/dynamic"]}}
                if scoped
                else {}
            )
            h.config(
                name,
                entries=[h.site.url + "/dynamic/cross-parent"],
                output_schema="record.v1",
                robots=True,
                delay=0.0,
                allowed_hosts=["127.0.0.1", "127.0.0.2"],
                allowed_path_prefixes=["/dynamic"],
                **extra,
            )
            cross_binding = h.binding(name, version, params)
            cross = h.cli("collect", name, "--binding", cross_binding, ok=scoped)
            cross_inspection = h.cli("inspect", "run", cross["run_id"])
            cross_export = h.cli("export", name, "--run", cross["run_id"])
            h.capture(name, {"receipt": cross, "inspect": cross_inspection, "export": cross_export})
            h.check(
                name + "_record_matches_explicit_scope",
                any(r["record_type"] == "document_attachment" for r in cross_export["records"]),
                scoped,
            )
            final = next(
                e for e in cross_inspection["discovery"]["events"] if e["url"].endswith("?id=pdf")
            )
            h.check(name + "_download_evidence_retained", final["snapshot_id"] is not None)
            if not scoped:
                h.check(
                    name + "_explicit_denial_reason",
                    "attachment_host_path_required" in cross["report"]["errors"],
                )
                h.check(
                    name + "_ledger_scope_rejection_retained", final["scope_decision"], "rejected"
                )
                h.check(
                    name + "_ledger_denial_reason_retained",
                    final["reason"],
                    "attachment_host_path_required",
                )
            before = len(h.site.ledger)
            cross_replay = h.cli("replay", cross["run_id"], ok=scoped)
            h.check(name + "_replay_zero_network", len(h.site.ledger), before)
            h.check(
                name + "_replay_zero_http_attempts",
                cross_replay["summary"]["counts"]["http_attempts"],
                0,
            )
    finally:
        handler.do_GET = original
        cross_server.shutdown()
        cross_server.server_close()
        cross_thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    h, error = Harness(args.output), None
    try:
        h.start()
        exercise(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("v1.3-dynamic-attachment", error)
        path = args.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(
            version="v1.3",
            command="uv run --frozen python experiments/v1.3-acceptance/dynamic_attachment_acceptance.py --output <new-directory>",
        )
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        h.close()
    print(args.output.resolve())
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
