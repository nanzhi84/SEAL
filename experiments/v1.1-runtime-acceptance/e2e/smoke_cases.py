"""Independent fixture oracles and runtime behavior cases."""

import json
from pathlib import Path

from .smoke_support import PACK, sha

RECIPE = Path("experiments/v1.1-runtime-acceptance/e2e/smoke_recipe")


def semantic(documents):
    return [{k: d[k] for k in ("url", "title", "body", "date", "attachments")} for d in documents]


def golden(s):
    h = s.h
    recipe = h.cli("recipe", "pack", RECIPE)["recipe_version"]
    runs = {}
    for case in json.loads((PACK / "golden_expected.json").read_text())["cases"]:
        identity = case["id"]
        with s.case(identity, case["focus"]) as row:
            row["not_applicable_fields"] = {
                field: {
                    "status": "NOT_APPLICABLE",
                    "reason": "generic_document.v1 has no such output field",
                    "expected": case["expected"][field],
                }
                for field in ("update_date", "document_no")
            }
            if "server_revision" in case:
                s.control("revision?value=" + case["server_revision"])
            source = "golden_" + identity.lower()
            binding = s.bind(source, [case["url_path"]], recipe)
            run, data = s.execute(source, binding)
            runs[identity] = (case, source, binding, run, data)
            s.lineage(source, run, data)
            observations = [o for o in run["observations"] if o["url"].endswith(case["url_path"])]
            h.check(
                identity + "_archived_fixture_sha",
                [o["body_hash"] for o in observations],
                [case["fixture_sha256"]],
            )
            s.object(case["fixture_sha256"])
            row["expected_projection"] = {
                k: case["expected"][v]
                for k, v in {
                    "title": "title",
                    "date": "publication_date",
                    "body": "content",
                }.items()
            }
            row["actual_projection"] = [
                {k: d[k] for k in ("title", "date", "body")} for d in data["documents"]
            ]
            h.check(
                identity + "_semantic_projection",
                row["actual_projection"],
                [row["expected_projection"]],
            )
            h.check(identity + "_complete", run["run"]["status"], "complete")
    return recipe, runs


def runtime(s, recipe, golden_runs, baseline):
    h = s.h
    cases = {c["id"]: c for c in json.loads((PACK / "runtime_cases.json").read_text())["cases"]}
    simple = json.loads((PACK / "golden_expected.json").read_text())["cases"][0]
    with s.case("R01", cases["R01"]["name"]):
        binding = s.bind("pages", ["/list/1"], recipe, role="list")
        run, data = s.execute("pages", binding)
        expected = sorted(
            s.url + path
            for path in (
                "/notice/simple",
                "/notice/alternate",
                "/notice/date-trap",
                "/notice/table",
            )
        )
        h.check("four_declared_references", sorted(run["run"]["refs"]), expected)
        h.check(
            "four_detail_observations",
            sorted(o["url"] for o in run["observations"] if "/notice/" in o["url"]),
            expected,
        )
        h.check(
            "both_list_pages_archived",
            sorted(o["url"] for o in run["observations"] if "/list/" in o["url"]),
            [s.url + "/list/1", s.url + "/list/2"],
        )
        h.check("four_details_technically_complete", run["run"]["status"], "complete")
        s.lineage("pages", run, data)
    with s.case("R02", cases["R02"]["name"]):
        binding = s.bind("changing", ["/changing"], recipe)
        histories = []
        for version in "ABA":
            s.control("revision?value=" + version)
            run, data = s.execute("changing", binding)
            h.check("revision_run_complete", run["run"]["status"], "complete")
            histories.append((run, data))
            s.lineage("changing", run, data)
        state = h.cli("inspect", "source", "changing")
        revisions = state["revisions"]
        expected = [
            sha((PACK / "fixtures" / f"notice_change_{v.lower()}.html").read_bytes()) for v in "ABA"
        ]
        h.check("three_ordered_revision_hashes", [r["body_hash"] for r in revisions], expected)
        h.check(
            "predecessor_chain",
            [r["predecessor"] for r in revisions],
            [None, revisions[0]["id"], revisions[1]["id"]],
        )
        h.check("three_distinct_observations", len({r["observation_id"] for r in revisions}), 3)
        h.check("only_two_body_objects", len({r["body_hash"] for r in revisions}), 2)
        h.check(
            "a_result_reused",
            histories[0][1]["documents"][0]["result_id"],
            histories[2][1]["documents"][0]["result_id"],
        )
        for run, original in histories:
            later = h.cli("export", "changing", "--run", run["run"]["id"])
            h.check(
                "historical_run_lineage_immutable",
                [
                    {k: d[k] for k in ("result_id", "revision_id", "observation_id", "body_hash")}
                    for d in later["documents"]
                ],
                [
                    {k: d[k] for k in ("result_id", "revision_id", "observation_id", "body_hash")}
                    for d in original["documents"]
                ],
            )
    with s.case("R03", cases["R03"]["name"]):
        binding = s.bind("repeat", ["/notice/simple"], recipe)
        first, a = s.execute("repeat", binding)
        second, b = s.execute("repeat", binding)
        state = s.lineage("repeat", second, b)
        h.check(
            "one_revision_two_observations",
            [len(state["revisions"]), len(first["observations"]) + len(second["observations"])],
            [1, 2],
        )
        h.check(
            "same_body_and_result",
            [a["documents"][0][k] for k in ("body_hash", "result_id")],
            [b["documents"][0][k] for k in ("body_hash", "result_id")],
        )
        h.check(
            "different_observation",
            a["documents"][0]["observation_id"] != b["documents"][0]["observation_id"],
        )
    for identity, path, statuses in (
        ("R04", "/compressed", [200]),
        ("R05", "/redirect", [302, 200]),
        ("R06", "/flaky", [503, 200]),
    ):
        with s.case(identity, cases[identity]["name"]):
            binding = s.bind(identity.lower(), [path], recipe)
            run, data = s.execute(identity.lower(), binding)
            h.check(
                "response_chain:" + identity, [o["status"] for o in run["observations"]], statuses
            )
            h.check("final_response_parsed:" + identity, run["run"]["status"], "complete")
            h.check(
                "decoded_body:" + identity,
                data["documents"][0]["body_hash"],
                simple["fixture_sha256"],
            )
            s.lineage(identity.lower(), run, data)
    for identity, path, fixture in (
        ("R07", "/notice/missing-title", "notice_missing_title.html"),
        ("R08", "/empty", None),
    ):
        with s.case(identity, cases[identity]["name"]):
            binding = s.bind(identity.lower(), [path], recipe)
            run, data = s.execute(identity.lower(), binding)
            h.check("negative_is_partial:" + identity, run["run"]["status"], "partial")
            h.check(
                "negative_diagnostic:" + identity,
                "ambiguous_or_missing_field" in run["run"]["errors"],
            )
            h.check("negative_no_fabricated_result:" + identity, data["documents"], [])
            expected = sha((PACK / "fixtures" / fixture).read_bytes() if fixture else b"")
            h.check(
                "negative_archived:" + identity,
                [o["body_hash"] for o in run["observations"]],
                [expected],
            )
            s.object(expected)
    with s.case("R09", cases["R09"]["name"]):
        default = s.bind("candidate", ["/notice/simple"], recipe, params={"marker": "default"})
        h.select("candidate", default, 0)
        candidate = h.binding("candidate", recipe=recipe, params={"marker": "candidate"})
        before = h.cli("inspect", "source", "candidate")["source"]
        run, data = s.execute("candidate", candidate)
        after = s.lineage("candidate", run, data)["source"]
        h.check("candidate_is_different", candidate != default)
        h.check("candidate_completed", run["run"]["status"], "complete")
        h.check(
            "default_and_generation_unchanged",
            [after["binding_id"], after["generation"]],
            [before["binding_id"], before["generation"]],
        )
        h.check("candidate_export_binding", data["documents"][0]["binding_id"], candidate)
    with s.case("R11", cases["R11"]["name"]):
        for identity, (case, source, binding, sync, a) in golden_runs.items():
            if "server_revision" in case:
                s.control("revision?value=" + case["server_revision"])
            queued, b = s.execute(source, binding, queue=True)
            s.lineage(source, queued, b)
            h.check("sync_queue_status:" + identity, queued["run"]["status"], sync["run"]["status"])
            h.check("sync_queue_errors:" + identity, queued["run"]["errors"], sync["run"]["errors"])
            h.check(
                "sync_queue_semantics:" + identity,
                semantic(b["documents"]),
                semantic(a["documents"]),
            )
    with s.case("R12", cases["R12"]["name"]) as row:
        assertions = json.loads((baseline / "assertions.json").read_text())
        names = {
            "killed_worker_recovered",
            "stalled_recovery_new_attempt",
            "stalled_recovery_complete_once",
            "old_run_cannot_reclaim_write_seq",
            "body_crash_recovered",
            "completed_crash_recovered",
            "committed_run_not_downloaded_again",
            "body_crash_no_duplicate_events",
            "completed_crash_no_duplicate_events",
            "logical_run_has_three_attempt_limit",
        }
        names.update(
            "late_old_" + kind + "_does_not_overwrite" for kind in ("run", "attempt", "generation")
        )
        selected = [a for a in assertions if a["name"] in names]
        row["reused_evidence"] = str(baseline)
        row["baseline_assertions_sha256"] = sha((baseline / "assertions.json").read_bytes())
        row["baseline_assertions"] = selected
        h.check("all_fault_assertions_present", sorted(a["name"] for a in selected), sorted(names))
        h.check(
            "all_fault_assertions_pass",
            all(a["status"] == "PASS" and a["actual"] == a["expected"] for a in selected),
        )
    with s.case("X01", "No automatic robots; explicit business HTTP errors remain failures"):
        binding = s.bind("robots_missing", ["/notice/simple"], recipe, robots=True)
        run, data = s.execute("robots_missing", binding)
        h.check(
            "no_automatic_robots_observation",
            not any(
                o["url"].endswith("/robots.txt") and o["status"] == 404 for o in run["observations"]
            ),
        )
        h.check("robots_missing_document_complete", run["run"]["status"], "complete")
        s.lineage("robots_missing", run, data)
        binding = s.bind("business_missing", ["/not-found"], recipe)
        missing, data = s.execute("business_missing", binding)
        h.check("business_404_still_error", "http_error" in missing["run"]["errors"])
        h.check("business_404_no_result", data["documents"], [])
        binding = s.bind("robots_as_document", ["/robots.txt"], recipe)
        declared, data = s.execute("robots_as_document", binding)
        h.check("declared_robots_404_still_error", "http_error" in declared["run"]["errors"])
    with s.case("R10", cases["R10"]["name"]):
        from .smoke_locator_contracts import replay_all

        replay_all(s, recipe, golden_runs)
