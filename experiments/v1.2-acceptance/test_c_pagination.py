"""C pagination behavior through actual CLI, HTTP and isolated PostgreSQL."""

from live_verify import verify_export
from pagination_fixture import API_PATH, PaginationSite, trustees
from runtime_support import by_key, run


def source(h, name, version, *, expected=25, max_pages=10, requests=10, params=None, entry_page=1):
    h.config(
        name,
        entries=[h.site.url + API_PATH + f"?pageNo={entry_page}&pageSize=10"],
        allowed_path_prefixes=[API_PATH],
        identity="business_key",
        output_schema="record.v1",
        seed_role="api",
        concurrency=1,
        delay=0.0,
        budget={"requests": requests, "seconds": 90, "response_bytes": 100000},
    )
    return h.binding(
        name, recipe=version, params=params or {"expected_total": expected, "max_pages": max_pages}
    )


def oracle(total):
    return {
        row["trustName"]: {k: row[k] for k in ("trustName", "regAddr")} for row in trustees(total)
    }


def evidence(h, name, exported):
    h.check(name + "_independent_raw_fields", verify_export(h.root / "archive", exported), [])
    h.check(
        name + "_no_fabricated_detail",
        all(
            r["detail_url"] is None and r["frozen_parent_request"]["role"] == "api"
            for r in exported["records"]
        ),
    )


def full(h, version):
    binding = source(h, "full_pages", version)
    before = len(h.site.ledger)
    receipt, inspected, initial = run(h, "full_pages", binding, capture="pagination-full-first")
    h.check("full_enumeration_complete", receipt["status"], "complete")
    h.check("full_enumeration_count", len(initial["records"]), 25)
    h.check(
        "full_enumeration_business_oracle",
        {k: v["data"] for k, v in by_key(initial).items()},
        oracle(25),
    )
    h.check("full_terminal_pages", [x["page"] for x in h.site.ledger[before:]], [1, 2, 3])
    h.check("full_terminal_tail", h.site.ledger[-1]["rows"], 5)
    evidence(h, "full_first", initial)
    versions = {k: r["record_version_id"] for k, r in by_key(initial).items()}
    h.site.state = "reorder"
    second, _, current = run(h, "full_pages", binding, capture="pagination-full-reordered")
    h.check("full_reorder_complete", second["status"], "complete")
    h.check(
        "full_reorder_same_versions",
        {k: r["record_version_id"] for k, r in by_key(current).items()},
        versions,
    )
    h.check(
        "full_reorder_same_values", {k: r["data"] for k, r in by_key(current).items()}, oracle(25)
    )
    evidence(h, "full_reordered", current)
    before = len(h.site.ledger)
    checked, check_inspect, rechecked = run(
        h, "full_pages", binding, recheck=True, capture="pagination-full-recheck"
    )
    h.check("full_recheck_complete", checked["status"], "complete")
    h.check("full_recheck_frozen_api_seeds", len(check_inspect["run"]["seeds"]), 3)
    h.check(
        "full_recheck_native_http_unique",
        sorted(x["page"] for x in h.site.ledger[before:]),
        [1, 2, 3],
    )
    h.check(
        "full_recheck_chain_duplicates_retained", check_inspect["discovery"]["deduplicated"] >= 2
    )
    evidence(h, "full_recheck", rechecked)
    before = len(h.site.ledger)
    replay = h.cli("replay", receipt["run_id"])
    replay_inspect = h.cli("inspect", "run", replay["run_id"])
    replayed = h.cli("export", "full_pages", "--run", replay["run_id"])
    h.check("full_replay_complete", replay["status"], "complete")
    h.check("full_replay_zero_http", len(h.site.ledger), before)
    h.check("full_replay_zero_observations", replay_inspect["observations"], [])
    h.check(
        "full_replay_original_values",
        {k: r["data"] for k, r in by_key(replayed).items()},
        oracle(25),
    )
    h.capture(
        "pagination-full-replay", {"receipt": replay, "inspect": replay_inspect, "export": replayed}
    )
    evidence(h, "full_replay", replayed)
    h.site.state = "base"


def boundaries(h, version):
    for state, reason, count, pages in [
        ("total_changed", "pagination_total_changed", 10, 2),
        ("early_empty", "pagination_incomplete", 10, 2),
        ("short_tail", "pagination_incomplete", 20, 3),
        ("duplicate", "pagination_incomplete", 10, 2),
        ("invalid_total", "pagination_incomplete", 0, 1),
        ("envelope_failed", "pagination_incomplete", 0, 1),
    ]:
        h.site.state = state
        name = "pagination_" + state
        binding = source(h, name, version)
        before = len(h.site.ledger)
        receipt, inspected, exported = run(h, name, binding, ok=False, capture=name)
        h.check(name + "_partial", receipt["status"], "partial")
        h.check(name + "_specific_error", reason in receipt["errors"])
        h.check(name + "_preserved_count", len(exported["records"]), count)
        h.check(name + "_finite_requests", len(h.site.ledger) - before, pages)
        h.check(name + "_no_pending", inspected["discovery"]["pending"], 0)
        evidence(h, name, exported)
    h.site.state = "base"
    for name, limit, budget in [("pagination_max_pages", 2, 10), ("pagination_budget", 10, 2)]:
        binding = source(h, name, version, max_pages=limit, requests=budget)
        before = len(h.site.ledger)
        receipt, inspected, exported = run(h, name, binding, ok=False, capture=name)
        h.check(name + "_partial", receipt["status"], "partial")
        h.check(name + "_proactive_reason", "pagination_limit" in receipt["errors"])
        h.check(name + "_first_response_only", len(h.site.ledger) - before, 1)
        h.check(name + "_no_follow_discovery", inspected["discovery"]["discovered"], 1)
        h.check(name + "_no_false_prefix_full", exported["records"], [])
    for total, pages in [(0, 1), (20, 2)]:
        h.site.total = total
        name = "pagination_total_" + str(total)
        binding = source(h, name, version, expected=total)
        before = len(h.site.ledger)
        receipt, _, exported = run(h, name, binding, capture=name)
        h.check(name + "_complete", receipt["status"], "complete")
        h.check(name + "_exact_count", len(exported["records"]), total)
        h.check(name + "_terminal_without_extra_empty_http", len(h.site.ledger) - before, pages)
        evidence(h, name, exported)
    h.site.total = 25


def wrong_entry(h, version, *, red=False):
    binding = source(h, "pagination_wrong_entry", version, entry_page=2)
    before = len(h.site.ledger)
    receipt, _, exported = run(
        h, "pagination_wrong_entry", binding, ok=red, capture="pagination-wrong-entry"
    )
    if red:
        h.check("wrong_entry_old_truncated_rows", len(exported["records"]), 15)
        h.check("wrong_entry_old_started_page2", h.site.ledger[before]["page"], 2)
    h.check("wrong_entry_rejects_truncated_collect", receipt["status"], "partial")
    h.check("wrong_entry_specific_reason", "pagination_incomplete" in receipt["errors"])
    h.check("wrong_entry_no_records", exported["records"], [])
    h.check("wrong_entry_one_response_no_follow", len(h.site.ledger) - before, 1)


def generic(h):
    version = h.cli("recipe", "pack", "recipes/heterogeneous")["recipe_version"]
    for total in [0, 25]:
        h.site.total = total
        name = "json_scope_missing_" + str(total)
        binding = source(h, name, version, params={"mode": "json", "key_field": "trustName"})
        receipt, _, exported = run(h, name, binding, ok=False, capture=name)
        h.check(name + "_partial", receipt["status"], "partial")
        h.check(name + "_explicit_diagnostic", "pagination_contract_missing" in receipt["errors"])
        h.check(name + "_no_claimed_full", exported["records"], [])
        declared = source(
            h,
            "json_single_page_" + str(total),
            version,
            params={"mode": "json", "json_scope": "single_page", "key_field": "trustName"},
        )
        receipt, _, exported = run(
            h, "json_single_page_" + str(total), declared, capture="json-single-page-" + str(total)
        )
        h.check(name + "_declared_single_complete", receipt["status"], "complete")
        h.check(name + "_declared_page_count", len(exported["records"]), min(total, 10))
    h.site.total = 25


def run_all(h, baseline=False, wrong_entry_red=False):
    old = h.site
    site = PaginationSite()
    h.site = site
    try:
        if wrong_entry_red:
            version = h.cli("recipe", "pack", "recipes/amac_full")["recipe_version"]
            wrong_entry(h, version, red=True)
        elif baseline:
            version = h.cli("recipe", "pack", "recipes/heterogeneous")["recipe_version"]
            binding = source(
                h, "pagination_red", version, params={"mode": "json", "key_field": "trustName"}
            )
            receipt, _, exported = run(
                h, "pagination_red", binding, capture="pagination-red-truncated"
            )
            h.check("red_old_single_page_claims_complete", receipt["status"], "complete")
            h.check("red_full_population_count", len(exported["records"]), 25)
        else:
            version = h.cli("recipe", "pack", "recipes/amac_full")["recipe_version"]
            full(h, version)
            boundaries(h, version)
            wrong_entry(h, version)
            generic(h)
    finally:
        h.pagination_ledger = site.ledger
        site.close()
        h.site = old
