"""Frozen Recheck boundaries and once-per-policy discovery via real CLI/PG/HTTP."""

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/v1.2-acceptance"))
from acceptance import Harness  # noqa: E402
from runtime_support import sql  # noqa: E402


def exercise(h):
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    rechecking = [False]
    document_paths = [f"/documents/{number:03d}" for number in range(10)]
    sitemap_paths = [f"/maps/{number:02d}.xml" for number in range(20)]

    def get(request):
        path = request.path
        content_type = "text/html; charset=utf-8"
        if path == "/robots.txt":
            content_type = "text/plain"
            # A repeated declaration in one policy must also produce one event.
            body = "User-agent: *\nAllow: /\n" + "".join(
                "Sitemap: " + h.site.url + url + "\n" for url in sitemap_paths + sitemap_paths[:1]
            )
        elif path in sitemap_paths:
            content_type = "application/xml"
            body = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
        elif path in document_paths:
            body = (
                "<html><h1>Public regulation "
                + path.rsplit("/", 1)[-1]
                + "</h1><article><h2>第一条 公开规定</h2><p>"
                + "这是确定性复核样本，保留公开正文和版本证据。" * 10
                + "</p></article>"
            )
            if rechecking[0]:
                body += "<nav>" + "".join(
                    f'<a href="/unplanned/{number}">Other document</a>'
                    f'<a href="https://outside.invalid/{number}">Outside document</a>'
                    for number in range(200)
                )
                body += (
                    '<a href="/unplanned/asset.pdf">Attachment</a>'
                    '<iframe src="/unplanned/frame"></iframe>'
                    '<link rel="sitemap" href="/unplanned/map.xml"></nav>'
                )
            body += "</html>"
        else:
            return original(request)
        encoded = body.encode()
        h.site.ledger.append({"method": "GET", "path": path, "status": 200})
        request.send_response(200)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(encoded)))
        request.end_headers()
        request.wfile.write(encoded)

    handler.do_GET = get
    try:
        recipe = h.cli("recipe", "pack", "recipes/seeded")["recipe_version"]
        config = {
            "entries": [h.site.url + path for path in document_paths],
            "output_schema": "record.v1",
            "seed_role": "detail",
            "robots": True,
            "delay": 0.0,
        }
        h.config("review_boundaries", **config)
        params = {"expand_homepage": False, "discover_sitemaps": True}
        binding = h.binding("review_boundaries", recipe, params)
        first = h.cli("collect", "review_boundaries", "--binding", binding)
        first_inspection = h.cli("inspect", "run", first["run_id"])
        events = first_inspection["discovery"]["events"]
        policy_events = [event for event in events if event["discovery_method"] == "robots"]
        sitemaps = [event for event in events if event["discovery_method"] == "robots_sitemap"]
        h.check("ten_document_collect_complete", first["status"], "complete")
        h.check("ten_known_record_identities", first["summary"]["counts"]["records"], 10)
        h.check("one_policy_fetch_for_many_pages", len(policy_events), 1)
        h.check("twenty_policy_sitemaps_have_only_twenty_candidates", len(sitemaps), 20)
        h.check("policy_sitemap_urls_not_repeated", len({event["url"] for event in sitemaps}), 20)
        policy = policy_events[0]
        h.check(
            "policy_candidates_retain_first_archived_parent",
            all(
                event["parent_url"] == h.site.url + "/robots.txt"
                and event["parent_snapshot_id"] == policy["snapshot_id"]
                and event["parent_observation_id"] == policy["observation_id"]
                for event in sitemaps
            ),
        )
        h.check(
            "each_policy_sitemap_downloaded_once",
            all(sum(row["path"] == path for row in h.site.ledger) == 1 for path in sitemap_paths),
        )
        before_collect_replay = len(h.site.ledger)
        collect_replay = h.cli("replay", first["run_id"])
        collect_replay_events = h.cli("inspect", "run", collect_replay["run_id"])["discovery"][
            "events"
        ]
        h.check("once_per_policy_collect_replay_complete", collect_replay["status"], "complete")
        h.check(
            "once_per_policy_collect_replay_zero_http", len(h.site.ledger), before_collect_replay
        )
        h.check(
            "once_per_policy_replay_keeps_twenty_candidates",
            sum(event["discovery_method"] == "robots_sitemap" for event in collect_replay_events),
            20,
        )

        # Only one of the ten identities is due. Two HTTP attempts are enough
        # for its business target and the explicitly permitted robots guard.
        h.config(
            "review_boundaries",
            **config,
            budget={"requests": 2, "seconds": 25, "response_bytes": 100000},
        )
        binding = h.binding("review_boundaries", recipe, params)
        source = h.cli("inspect", "source", "review_boundaries")["source"]
        h.select("review_boundaries", binding, source["generation"])
        sql(
            h,
            "UPDATE seal_source SET next_poll=now()+interval '1 day' WHERE id='review_boundaries'",
        )
        sql(
            h,
            "UPDATE seal_record SET next_check=now()+interval '1 day' "
            "WHERE source_id='review_boundaries'",
        )
        due_url = h.site.url + document_paths[0]
        sql(
            h,
            "UPDATE seal_record SET next_check=now()-interval '1 hour' "
            "WHERE source_id='review_boundaries' AND record_key=%s",
            (due_url,),
        )
        before_dates = sql(
            h,
            "SELECT record_key,next_check FROM seal_record WHERE source_id='review_boundaries' "
            "ORDER BY record_key",
        )
        scheduled = h.cli("schedule")
        h.check("one_due_source_run_scheduled", len(scheduled["run_ids"]), 1)
        run_id = scheduled["run_ids"][0]
        planned = h.cli("inspect", "run", run_id)["run"]
        h.check(
            "only_one_frozen_recheck_target",
            planned["seeds"],
            [{"url": due_url, "role": "detail", "method": "GET"}],
        )
        h.check("only_one_due_record_in_frozen_plan", len(planned["recheck_plan"]["records"]), 1)
        rechecking[0] = True
        before_http = len(h.site.ledger)
        h.start_worker()
        h.wait_worker()
        inspection = h.cli("inspect", "run", run_id)
        result = inspection["run"]
        events = inspection["discovery"]["events"]
        h.check("due_document_recheck_complete", result["status"], "complete")
        h.check(
            "two_http_attempts_only_for_target_and_policy",
            sorted(row["path"] for row in h.site.ledger[before_http:]),
            ["/documents/000", "/robots.txt"],
        )
        h.check(
            "actual_scheduler_urls_equal_frozen_targets_plus_policy",
            {event["url"] for event in events},
            {due_url, h.site.url + "/robots.txt"},
        )
        h.check("no_new_sitemap_or_link_discovery", len(events), 2)
        h.check("unplanned_links_cannot_exhaust_budget", result["errors"], [])
        h.check(
            "recheck_observed_its_only_planned_identity",
            result["report"]["scope_evidence"]["unobserved_record_count"],
            0,
        )
        h.check(
            "recheck_cannot_add_unknown_record_identities",
            sql(
                h,
                "SELECT count(*) AS n FROM seal_record WHERE source_id='review_boundaries' AND namespace='runtime'",
            )[0]["n"],
            10,
        )
        after_dates = sql(
            h,
            "SELECT record_key,next_check FROM seal_record WHERE source_id='review_boundaries' "
            "ORDER BY record_key",
        )
        h.check("unplanned_nine_record_dates_unchanged", after_dates[1:], before_dates[1:])
        h.check(
            "planned_record_due_date_advanced",
            after_dates[0]["next_check"] > before_dates[0]["next_check"],
        )
        before_replay = len(h.site.ledger)
        replay = h.cli("replay", run_id)
        replay_events = h.cli("inspect", "run", replay["run_id"])["discovery"]["events"]
        h.check("recheck_replay_complete", replay["status"], "complete")
        h.check("recheck_replay_zero_http", len(h.site.ledger), before_replay)
        h.check(
            "recheck_replay_keeps_frozen_discovery_boundary",
            {event["url"] for event in replay_events},
            {due_url, h.site.url + "/robots.txt"},
        )
        h.capture(
            "frozen-review-boundaries",
            {
                "collect": first_inspection,
                "collect_replay": collect_replay,
                "planned": planned,
                "recheck": inspection,
                "replay": replay,
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
        exercise(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("v1.3-review-boundaries", error)
        path = args.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(
            version="v1.3",
            command="uv run --frozen python experiments/v1.3-acceptance/review_boundaries_acceptance.py --output <new-directory>",
        )
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        h.close()
    print(args.output.resolve())
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
