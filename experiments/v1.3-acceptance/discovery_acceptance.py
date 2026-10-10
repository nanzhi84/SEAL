"""Actual CLI/PG/HTTP contracts for seeded recursion, robots and Replay."""

import argparse
import html
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/v1.2-acceptance"))
from acceptance import Harness  # noqa: E402


def exercise(h):
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    wire_paths = [
        "/seed/d!go?q=!",
        "/seed/d%21go?q=%21",
        "/seed/d?q=a+b",
        "/seed/d?q=a%20b",
        "/seed/d?a=1&a=2",
        "/seed/d?a=2&a=1",
        "/seed/d?q=%2f",
        "/seed/d?q=%2F",
        "/seed/d?bare&empty=&x=1&&",
    ]
    policy_status = [200]
    policy_content = [None]

    def get(request):
        path = request.path
        status, content_type = 200, "text/html; charset=utf-8"
        if path == "/robots.txt":
            status, content_type = policy_status[0], "text/plain"
            body = (
                (
                    "User-agent: *\nDisallow: /seed/denied\nSitemap: "
                    + h.site.url
                    + "/seed/map.xml\n"
                )
                if status == 200
                else ""
            )
            if policy_content[0] is not None:
                body = policy_content[0]
        elif path in ("/", "/sitemap.xml"):
            status, body = 404, "missing optional entry"
        elif path == "/seed/list":
            body = (
                "<html><nav>"
                + "".join(
                    '<a href="' + html.escape(p, quote=True) + '">Public</a>'
                    for p in wire_paths
                    + wire_paths[:1]
                    + [
                        "/seed/list2",
                        "/seed/denied",
                        "/outside/page",
                        "/seed/private",
                        "https://outside.invalid/x",
                        "/seed/file.txt",
                    ]
                )
                + "</nav></html>"
            )
        elif path == "/seed/list2":
            body = '<html><nav><a href="/seed/list">Previous</a><a href="/seed/final">Next</a></nav></html>'
        elif path == "/seed/map.xml":
            content_type = "application/xml"
            body = (
                '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>'
                + h.site.url
                + "/seed/child.xml</loc></sitemap></sitemapindex>"
            )
        elif path == "/seed/iframe":
            body = (
                '<html><iframe src="'
                + h.site.url.replace("127.0.0.1", "localhost")
                + '/seed/final"></iframe></html>'
            )
        elif path == "/seed/child.xml":
            content_type = "application/xml"
            body = (
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'
                + h.site.url
                + "/seed/from-map</loc></url></urlset>"
            )
        elif path.startswith("/seed/trap"):
            number = int(path.partition("n=")[2] or "0")
            body = '<html><nav><a href="/seed/trap?n=' + str(number + 1) + '">Next</a></nav></html>'
        elif path == "/seed/file.txt":
            content_type, body = (
                "text/plain",
                "Long public attachment. Legal text retained from the original response bytes.",
            )
        elif path.startswith("/seed/"):
            body = (
                "<html><h1>"
                + html.escape(path)
                + "</h1><article><h2>第一条 公开规定</h2><p>这是一项公开信息采集的确定性工程样本，正文和编号必须保留，并且通过归档原文核对字段定位。</p></article></html>"
            )
        else:
            return original(request)
        encoded = body.encode()
        h.site.ledger.append({"method": "GET", "path": path, "status": status})
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(encoded)))
        request.end_headers()
        request.wfile.write(encoded)

    handler.do_GET = get
    try:
        recipe = h.cli("recipe", "pack", "recipes/seeded")["recipe_version"]
        h.config(
            "seeded",
            entries=[h.site.url + "/seed/list"],
            allowed_path_prefixes=["/seed"],
            output_schema="record.v1",
            robots=True,
            delay=0.0,
        )
        binding = h.binding(
            "seeded",
            recipe,
            {
                "exclude_patterns": ["*/private*"],
                "specific_rules": [
                    {"path_regex": "/seed/final$", "title": "h1", "body": "article", "date": None}
                ],
            },
        )
        receipt = h.cli("collect", "seeded", "--binding", binding, ok=False)
        inspection = h.cli("inspect", "run", receipt["run_id"])
        export = h.cli("export", "seeded", "--run", receipt["run_id"])
        h.capture("seeded-recursion", {"receipt": receipt, "inspect": inspection, "export": export})
        paths = [row["path"] for row in h.site.ledger]
        h.check("robots_exact_policy_fetch_once", paths.count("/robots.txt"), 1)
        h.check("robots_denied_not_downloaded", "/seed/denied" in paths, False)
        h.check("scope_path_not_downloaded", "/outside/page" in paths, False)
        h.check("recipe_exclusion_not_downloaded", "/seed/private" in paths, False)
        h.check("recursive_second_page_fetched", "/seed/list2" in paths)
        h.check("recursive_final_fetched", "/seed/final" in paths)
        h.check(
            "sitemap_index_to_urlset_to_content",
            all(p in paths for p in ["/seed/map.xml", "/seed/child.xml", "/seed/from-map"]),
        )
        h.check("semantic_wire_spellings", all(paths.count(p) == 1 for p in wire_paths))
        h.check("attachment_fetched", "/seed/file.txt" in paths)
        events = inspection["discovery"]["events"]
        h.check(
            "scope_rejection_evidence",
            any(
                e["reason"] == "request_out_of_scope" and e["scope_decision"] == "rejected"
                for e in events
            ),
        )
        h.check("robots_rejection_evidence", any(e["reason"] == "robots_denied" for e in events))
        h.check(
            "native_duplicates_recorded", any(e["reason"] == "duplicate_request" for e in events)
        )
        sitemap_event = next(e for e in events if e["discovery_method"] == "robots_sitemap")
        h.check(
            "robots_sitemap_policy_parent_url",
            sitemap_event["parent_url"],
            h.site.url + "/robots.txt",
        )
        h.check(
            "robots_sitemap_policy_parent_archive",
            bool(sitemap_event["parent_snapshot_id"] and sitemap_event["parent_observation_id"]),
        )
        h.check(
            "no_policy_record",
            all(r["data"].get("url") != h.site.url + "/robots.txt" for r in export["records"]),
        )
        h.check("effective_defaults_frozen", receipt["report"]["seeded_policy"]["max_depth"], 12)
        h.check(
            "specific_rule_used", receipt["report"]["stats"].get("seal/specific_documents", 0) >= 1
        )
        h.check(
            "fallback_used_for_unknown_templates",
            receipt["report"]["stats"].get("seal/fallback_documents", 0) >= 1,
        )
        before = len(h.site.ledger)
        replay = h.cli("replay", receipt["run_id"], ok=False)
        replay_export = h.cli("export", "seeded", "--run", replay["run_id"])
        h.check("replay_zero_http_including_robots", len(h.site.ledger), before)
        h.check(
            "replay_structured_output_deterministic",
            sorted(
                (r["data"] for r in replay_export["records"]),
                key=lambda d: json.dumps(d, sort_keys=True),
            ),
            sorted(
                (r["data"] for r in export["records"]), key=lambda d: json.dumps(d, sort_keys=True)
            ),
        )
        # Query enumeration and depth are independent conservative truncations.
        for name, params, reason in (
            (
                "query_bound",
                {"max_query_variants": 3, "max_depth": 30},
                "query_variant_budget_exceeded",
            ),
            ("depth_bound", {"max_query_variants": 50, "max_depth": 2}, "discovery_depth_exceeded"),
        ):
            h.config(
                name,
                entries=[h.site.url + "/seed/trap?n=0"],
                output_schema="record.v1",
                robots=True,
                delay=0.0,
            )
            frozen = h.binding(
                name, recipe, {**params, "expand_homepage": False, "discover_sitemaps": False}
            )
            run = h.cli("collect", name, "--binding", frozen, ok=False)
            h.check(name + "_is_partial", run["status"], "partial")
            h.check(name + "_has_explicit_reason", reason in run["errors"])
        h.config(
            "http_bound",
            entries=[h.site.url + "/seed/trap?n=0"],
            output_schema="record.v1",
            robots=True,
            delay=0.0,
            budget={"requests": 4, "seconds": 25, "response_bytes": 100000},
        )
        frozen = h.binding(
            "http_bound",
            recipe,
            {
                "max_query_variants": 100,
                "max_depth": 100,
                "expand_homepage": False,
                "discover_sitemaps": False,
            },
        )
        run = h.cli("collect", "http_bound", "--binding", frozen, ok=False)
        h.check("http_budget_is_partial", run["status"], "partial")
        h.check("http_budget_counts_policy", run["summary"]["counts"]["http_attempts"] <= 4)
        h.check("http_budget_explicit_reason", "request_budget_exceeded" in run["errors"])
        for status in (404, 403, 503):
            policy_status[0] = status
            name = "policy_" + str(status)
            h.config(
                name,
                entries=[h.site.url + "/seed/final"],
                output_schema="record.v1",
                robots=True,
                delay=0.0,
            )
            frozen = h.binding(name, recipe, {"expand_homepage": False, "discover_sitemaps": False})
            before = len(h.site.ledger)
            run = h.cli("collect", name, "--binding", frozen, ok=status == 404)
            fetched = [row["path"] for row in h.site.ledger[before:]]
            if status == 404:
                h.check("absent_policy_permits_public_content", "/seed/final" in fetched)
                h.check("absent_policy_not_http_error", "http_error" in run["errors"], False)
                h.check("absent_policy_complete", run["status"], "complete")
            else:
                h.check(name + "_fails_closed", "/seed/final" in fetched, False)
                h.check(
                    name + "_reason",
                    ("robots_denied" if status == 403 else "robots_unavailable") in run["errors"],
                )
        policy_status[0], policy_content[0] = (
            200,
            "<html><form>Sign in to verify access</form></html>",
        )
        h.config(
            "policy_challenge",
            entries=[h.site.url + "/seed/final"],
            output_schema="record.v1",
            robots=True,
            delay=0.0,
        )
        frozen = h.binding(
            "policy_challenge", recipe, {"expand_homepage": False, "discover_sitemaps": False}
        )
        before = len(h.site.ledger)
        run = h.cli("collect", "policy_challenge", "--binding", frozen, ok=False)
        h.check(
            "policy_200_html_challenge_fails_closed",
            "/seed/final" in [row["path"] for row in h.site.ledger[before:]],
            False,
        )
        h.check("policy_200_html_challenge_reason", "robots_unavailable" in run["errors"])
        policy_status[0], policy_content[0] = 404, None
        h.config(
            "optional_probe",
            entries=[h.site.url + "/seed/final"],
            output_schema="record.v1",
            robots=True,
            delay=0.0,
        )
        frozen = h.binding("optional_probe", recipe)
        run = h.cli("collect", "optional_probe", "--binding", frozen)
        h.check("optional_home_and_sitemap_missing_complete", run["status"], "complete")
        h.check(
            "optional_missing_entries_auditable",
            run["report"]["discovery"]["outcomes"].get("optional_entry_missing"),
            2,
        )
        before = len(h.site.ledger)
        replay = h.cli("replay", run["run_id"])
        h.check("optional_entry_replay_complete", replay["status"], "complete")
        h.check("optional_entry_replay_zero_http", len(h.site.ledger), before)
        h.config(
            "iframe_scope",
            entries=[h.site.url + "/seed/iframe"],
            output_schema="record.v1",
            robots=True,
            delay=0.0,
            allowed_hosts=["127.0.0.1", "localhost"],
            allowed_path_prefixes=["/seed"],
            host_path_scopes={"127.0.0.1": ["/seed"], "localhost": ["/seed"]},
        )
        frozen = h.binding(
            "iframe_scope", recipe, {"expand_homepage": False, "discover_sitemaps": False}
        )
        run = h.cli("collect", "iframe_scope", "--binding", frozen, ok=False)
        events = h.cli("inspect", "run", run["run_id"])["discovery"]["events"]
        event = next(e for e in events if e["reason"] == "iframe_out_of_scope")
        h.check("late_guard_iframe_decision_persisted", event["scope_decision"], "rejected")
        h.check("cross_host_iframe_not_archived", event["snapshot_id"], None)
    finally:
        handler.do_GET = original


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
        h.save("v1.3-seeded-discovery", error)
        path = args.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(
            version="v1.3",
            command="uv run --frozen python experiments/v1.3-acceptance/discovery_acceptance.py --output <new-directory>",
        )
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        h.close()
    print(args.output.resolve())
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
