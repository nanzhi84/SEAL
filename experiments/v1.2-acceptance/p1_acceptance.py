"""Real CLI duplicate-parent arrival permutations and stable archived lineage."""

import argparse
import json
import traceback
from pathlib import Path

from acceptance import Harness
from runtime_support import by_key, recipe, record_source, run, sql


def parents(h, version):
    expected = {"url": h.site.url + "/api/a", "role": "api", "method": "GET"}
    binding = record_source(h, "parentorder", version, paths=["/api/a", "/api/b"])
    previous = None
    for slow, fast in (("a", "b"), ("b", "a")):
        h.site.delays = {"/api/" + slow: 1.5}
        receipt, _, exported = run(h, "parentorder", binding, capture="arrival-" + fast)
        h.check("duplicate_collect_complete_" + fast, receipt["status"], "complete")
        emissions = sql(
            h,
            "SELECT rr.candidate->'frozen_parent_request'->>'url' AS url "
            "FROM seal_record_emission e JOIN seal_record_result rr ON rr.id=e.result_id "
            "WHERE e.run_id=%s ORDER BY e.created_at,e.id",
            (receipt["run_id"],),
        )
        h.check("actual_first_emission_" + fast, emissions[0]["url"], h.site.url + "/api/" + fast)
        h.check(
            "duplicate_count_" + fast, receipt["report"]["record_counts"]["duplicate_count"], 10
        )
        h.check(
            "both_evidences_" + fast,
            all(
                len(o["inputs"]) == len(o["record_result_ids"]) == 2
                for o in receipt["report"]["record_outputs"]
            ),
        )
        accepted = sql(
            h,
            "SELECT parent_request FROM seal_record WHERE source_id='parentorder' "
            "AND namespace='runtime'",
        )
        h.check(
            "stable_usable_parent_" + fast, [r["parent_request"] for r in accepted], [expected] * 10
        )
        identities = {
            k: (v["record_id"], v["record_version_id"], v["data"])
            for k, v in by_key(exported).items()
        }
        if previous is not None:
            h.check("arrival_permutation_same_records_versions", identities, previous)
        previous = identities
        count = len(h.site.ledger)
        replay = h.cli("replay", receipt["run_id"])
        h.check("replay_complete_" + fast, replay["status"], "complete")
        h.check("replay_no_network_" + fast, len(h.site.ledger), count)
        replayed = h.cli("export", "parentorder", "--run", replay["run_id"])
        h.check(
            "replay_parent_" + fast,
            [r["frozen_parent_request"] for r in replayed["records"]],
            [expected] * 10,
        )
        h.site.delays = {}
        count = len(h.site.ledger)
        checked, _, rechecked = run(
            h, "parentorder", binding, recheck=True, capture="recheck-" + fast
        )
        h.check("recheck_complete_" + fast, checked["status"], "complete")
        h.check(
            "recheck_actual_parent_" + fast,
            h.site.ledger[count:],
            [{"method": "GET", "path": "/api/a", "status": 200}],
        )
        h.check(
            "recheck_same_records_" + fast,
            {
                k: (v["record_id"], v["record_version_id"], v["data"])
                for k, v in by_key(rechecked).items()
            },
            previous,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", choices=["parents", "all"], default="all")
    args = parser.parse_args()
    h, error = Harness(args.output), None
    try:
        h.start()
        version = recipe(h)
        if args.scenario in ("all", "parents"):
            parents(h, version)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("p1-regressions", error)
        path = h.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(
            command="uv run --frozen python experiments/v1.2-acceptance/p1_acceptance.py "
            f"--scenario {args.scenario} --output <new-directory>",
            preconditions="Python 3.12, uv.lock; PostgreSQL 17+ tools on PATH; disposable local "
            "cluster and synthetic loopback HTTP only; no external DSN or real credentials",
        )
        path.write_text(json.dumps(manifest, indent=2))
        h.close()
    return int(error is not None)


if __name__ == "__main__":
    raise SystemExit(main())
