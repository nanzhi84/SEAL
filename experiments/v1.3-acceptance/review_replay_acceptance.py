"""Partial Crawl Replay with actual HTTP, CLI, PostgreSQL and immutable archives."""

import argparse
import html
import json
import shutil
import socket
import sys
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/v1.2-acceptance"))
from acceptance import Harness  # noqa: E402
from runtime_support import sql  # noqa: E402


def run_all(h):
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    recovery_attempts = 0

    def get(request):
        nonlocal recovery_attempts
        parsed = urlsplit(request.path)
        content_type = "text/html; charset=utf-8"
        if parsed.path == "/robots.txt":
            content_type, body = "text/plain", "User-agent: *\nAllow: /\n"
        elif parsed.path == "/partial/list":
            body = (
                "<html><nav>"
                + "".join(f'<a href="/partial/d{i}">Document {i}</a>' for i in range(20))
                + "</nav></html>"
            )
        elif parsed.path.startswith("/partial/d"):
            body = document(request.path)
        elif parsed.path.startswith("/depth/"):
            number = int(parsed.path.rsplit("/", 1)[-1])
            body = document(request.path).replace(
                "</html>", f'<a href="/depth/{number + 1}">Next</a></html>'
            )
        elif parsed.path == "/query":
            number = int(parse_qs(parsed.query).get("n", ["0"])[0])
            body = document(request.path).replace(
                "</html>", f'<a href="/query?n={number + 1}">Next</a></html>'
            )
        elif parsed.path == "/recover/list":
            body = '<html><nav><a href="/recover/detail">Public document</a></nav></html>'
        elif parsed.path == "/recover/detail":
            recovery_attempts += 1
            if recovery_attempts == 1:
                h.site.ledger.append(
                    {"method": "GET", "path": request.path, "error": "fixture_connection_closed"}
                )
                request.connection.shutdown(socket.SHUT_RDWR)
                request.connection.close()
                return
            body = document(request.path)
        else:
            return original(request)
        encoded = body.encode()
        h.site.ledger.append({"method": "GET", "path": request.path, "status": 200})
        request.send_response(200)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(encoded)))
        request.end_headers()
        request.wfile.write(encoded)

    def document(path):
        return (
            "<html><h1>Public document "
            + html.escape(path)
            + "</h1><article><p>公开文档正文，用来验证有限预算采集和离线复算。"
            "未归档输入必须保留未获取的原因，并且不得新增网络请求或假报完整覆盖。</p></article></html>"
        )

    handler.do_GET = get
    try:
        version = h.cli("recipe", "pack", "recipes/seeded")["recipe_version"]
        cases = (
            ("request", "/partial/list", {"requests": 7}, {}, "request_budget_exceeded"),
            ("depth", "/depth/0", {}, {"max_depth": 2}, "discovery_depth_exceeded"),
            (
                "query",
                "/query?n=0",
                {},
                {"max_query_variants": 3},
                "query_variant_budget_exceeded",
            ),
            # Guard admits the seed, then the auxiliary robots fetch exceeds the
            # same budget. No archived input exists, but the refusal is evidence.
            ("policy", "/partial/list", {"requests": 1}, {}, "request_budget_exceeded"),
        )
        collected = {}
        for name, path, budget, params, reason in cases:
            source = "replay_" + name
            h.config(
                source,
                entries=[h.site.url + path],
                output_schema="record.v1",
                robots=True,
                delay=0.0,
                concurrency=1,
                budget={"requests": 40, "seconds": 25, "response_bytes": 100000, **budget},
            )
            binding = h.binding(
                source, version, {"expand_homepage": False, "discover_sitemaps": False, **params}
            )
            receipt = h.cli("collect", source, "--binding", binding, ok=False)
            inspection = h.cli("inspect", "run", receipt["run_id"])
            export = h.cli("export", source, "--run", receipt["run_id"])
            h.check(name + "_collect_is_partial", receipt["status"], "partial")
            h.check(name + "_collect_has_original_reason", reason in receipt["errors"])
            h.check(name + "_collect_has_no_replay_miss", "replay_miss" in receipt["errors"], False)
            if name == "request":
                h.check("request_exactly_five_details_archived", len(export["records"]), 5)
                h.check(
                    "request_twenty_candidates_audited", len(inspection["discovery"]["events"]), 22
                )
            if name == "policy":
                h.check("policy_no_archived_inputs", len(inspection["run"]["inputs"]), 0)
            collected[name] = {
                "source": source,
                "receipt": receipt,
                "inspection": inspection,
                "export": export,
                "reason": reason,
            }

        h.config(
            "replay_recovered",
            entries=[h.site.url + "/recover/list"],
            output_schema="record.v1",
            robots=True,
            delay=0.0,
            concurrency=1,
        )
        recovery_binding = h.binding(
            "replay_recovered", version, {"expand_homepage": False, "discover_sitemaps": False}
        )
        recovered = h.cli("collect", "replay_recovered", "--binding", recovery_binding)
        recovered_inspection = h.cli("inspect", "run", recovered["run_id"])
        h.check("native_retry_recovered_original_complete", recovered["status"], "complete")
        h.check(
            "native_retry_original_failure_evidence",
            any(
                e["reason"] == "download_failed"
                for e in recovered_inspection["discovery"]["events"]
            ),
        )

        # Replay works with the origin shut down, including auxiliary policies.
        h.site.close()
        network_count = len(h.site.ledger)
        for name, case in collected.items():
            original_id = case["receipt"]["run_id"]
            replay = h.cli("replay", original_id, ok=False)
            inspection = h.cli("inspect", "run", replay["run_id"])
            export = h.cli("export", case["source"], "--run", replay["run_id"])
            h.check(name + "_replay_is_partial", replay["status"], "partial")
            h.check(name + "_replay_keeps_original_reason", case["reason"] in replay["errors"])
            h.check(name + "_replay_has_no_miss", "replay_miss" in replay["errors"], False)
            h.check(name + "_replay_no_network", len(h.site.ledger), network_count)
            h.check(
                name + "_replay_http_attempts_zero", replay["summary"]["counts"]["http_attempts"], 0
            )
            h.check(
                name + "_replay_only_original_archived_snapshots",
                {i["snapshot_id"] for i in inspection["run"]["inputs"]},
                {i["snapshot_id"] for i in case["inspection"]["run"]["inputs"]},
            )
            h.check(
                name + "_replay_data_deterministic",
                sorted(json.dumps(r["data"], sort_keys=True) for r in export["records"]),
                sorted(json.dumps(r["data"], sort_keys=True) for r in case["export"]["records"]),
            )
            h.check(
                name + "_original_frozen_report_unchanged",
                h.cli("inspect", "run", original_id)["run"]["report"],
                case["receipt"]["report"],
            )
            h.check(
                name + "_origin_contract_content_addressed",
                bool(replay["report"].get("replay_origin_id")),
            )
            if name == "request":
                h.check(
                    "request_replay_retains_fifteen_budget_refusals",
                    sum(
                        e["reason"] == "request_budget_exceeded"
                        for e in inspection["discovery"]["events"]
                    ),
                    15,
                )
            again = h.cli("replay", replay["run_id"], ok=False)
            h.check(name + "_replay_chain_partial", again["status"], "partial")
            h.check(name + "_replay_chain_keeps_original_reason", case["reason"] in again["errors"])
            h.check(name + "_replay_chain_no_miss", "replay_miss" in again["errors"], False)
            h.check(
                name + "_replay_chain_same_frozen_origin",
                again["report"]["replay_origin_id"],
                replay["report"]["replay_origin_id"],
            )
            h.check(name + "_replay_chain_no_network", len(h.site.ledger), network_count)
            case.update(replay=replay, replay_inspection=inspection, replay_again=again)

        # A reviewed replacement Recipe introduces one entirely new candidate.
        # Original refusals must never become a blanket URL/input whitelist.
        replacement = h.root / "replacement-recipe"
        shutil.copytree(ROOT / "recipes/seeded", replacement)
        path = replacement / "recipe.py"
        text = path.read_text()
        anchor = "        yield from self.policy_sitemaps(response)"
        text = text.replace(
            anchor,
            anchor
            + '\n        yield self.request(response.urljoin("/brand-new"), "detail", "html", parent=response)',
            1,
        )
        path.write_text(text)
        new_version = h.cli("recipe", "pack", replacement)["recipe_version"]
        case = collected["request"]
        binding = h.binding(
            case["source"], new_version, {"expand_homepage": False, "discover_sitemaps": False}
        )
        altered = h.cli("replay", case["receipt"]["run_id"], "--binding", binding, ok=False)
        h.check("new_recipe_candidate_remains_strict_miss", "replay_miss" in altered["errors"])
        h.check("new_recipe_candidate_no_network", len(h.site.ledger), network_count)
        h.check("new_recipe_keeps_original_partial_reason", case["reason"] in altered["errors"])

        # A failed native transport retry subsequently produced a Snapshot. It
        # cannot become a negative input that hides a missing archived mapping.
        # This fault injection touches only this suite's disposable synthetic DB.
        inputs = recovered_inspection["run"]["inputs"]
        without_detail = [item for item in inputs if not item["url"].endswith("/recover/detail")]
        sql(
            h,
            "UPDATE seal_run SET inputs=%s::jsonb WHERE id=%s",
            (json.dumps(without_detail), recovered["run_id"]),
        )
        try:
            missing = h.cli("replay", recovered["run_id"], ok=False)
            h.check(
                "recovered_transport_cannot_hide_missing_mapping",
                "replay_miss" in missing["errors"],
            )
            h.check(
                "recovered_transport_missing_mapping_no_network", len(h.site.ledger), network_count
            )
            h.check(
                "recovered_transport_not_frozen_as_negative_input",
                missing["report"].get("replay_origin_id"),
                None,
            )
        finally:
            sql(
                h,
                "UPDATE seal_run SET inputs=%s::jsonb WHERE id=%s",
                (json.dumps(inputs), recovered["run_id"]),
            )
        h.capture(
            "partial-replay",
            {
                "cases": collected,
                "new_recipe": altered,
                "recovered": recovered_inspection,
                "missing_recovered_input": missing,
            },
        )
    finally:
        handler.do_GET = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    h, error = Harness(args.output), None
    try:
        h.start()
        run_all(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("v1.3-review-partial-replay", error)
        h.close()
    print(json.dumps({"status": "FAIL" if error else "PASS", "assertions": len(h.assertions)}))
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
