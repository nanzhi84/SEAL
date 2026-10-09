import shutil


def m0(h):
    endpoints = ["redirect", "meta", "retry", "deflate", "pdf", "json", "txt"]
    h.config(
        "probe", [h.site.url + "/probe/" + p for p in endpoints], expected=False, seed_role="detail"
    )
    binding = h.binding("probe")
    trial = h.cli("trial", binding)
    h.check("native_crawl_complete", trial["status"], "complete")
    run = h.cli("inspect", "run", trial["run_id"])
    statuses = [o["status"] for o in run["observations"]]
    h.check("every_retry_archived", statuses.count(503), 2)
    h.check("redirect_archived", 302 in statuses)
    h.check(
        "pdf_text_matches_oracle",
        any("Synthetic PDF notice" in r["candidate"]["body"] for r in run["results"]),
    )
    h.check("callback_inputs_durable", all(r["checks"]["input_verified"] for r in run["results"]))
    before = len(h.site.ledger)
    replay = h.cli("replay", trial["run_id"])
    h.check("offline_replay_complete", replay["status"], "complete")
    h.check("replay_zero_requests", len(h.site.ledger), before)
    replay_run = h.cli("inspect", "run", replay["run_id"])
    h.check("replay_has_no_network_observations", len(replay_run["observations"]), 0)
    h.check(
        "replay_same_outputs",
        sorted(r["output_hash"] for r in run["results"]),
        sorted(r["output_hash"] for r in replay_run["results"]),
    )
    for endpoint in ("304", "429", "large", "badpdf", "missing"):
        h.config("bad", [h.site.url + "/probe/" + endpoint], expected=False, seed_role="detail")
        bad_binding = h.binding("bad")
        failed = h.cli("trial", bad_binding, ok=False)
        h.check(f"reject_{endpoint}", failed["status"] in ("partial", "failed"))
    h.config("sensitive")
    h.site.failure = "sensitive"
    failed = h.cli("trial", h.binding("sensitive"), ok=False)
    h.check("sensitive_body_blocked", failed["status"], "partial")
    h.site.failure = None
    h.check(
        "no_sensitive_values_in_archive",
        not any(
            b"synthetic-secret-do-not-archive" in p.read_bytes()
            or b"fixture-cookie=must-not-persist" in p.read_bytes()
            for p in (h.root / "archive").rglob("*")
            if p.is_file()
        ),
    )
    return binding


def m1(h):
    h.config()
    binding = h.binding()
    h.cli("activate", binding, "--expect-generation", "0", ok=False)
    h.check("unreviewed_binding_rejected", True)
    trial = h.cli("trial", binding)
    h.check("trial_isolated", len(h.export()["documents"]), 0)
    h.approve(binding, trial["run_id"])
    h.cli("activate", binding, "--expect-generation", "0")
    h.cli("activate", binding, "--expect-generation", "0", ok=False)
    h.check("activation_cas_conflict", True)
    run = h.cli("run", "a")
    exported = h.export()
    h.check(
        "published_expected_set",
        sorted(d["title"] for d in exported["documents"]),
        ["First notice", "Second notice"],
    )
    h.check(
        "publication_traceability",
        all(
            d["revision_id"] and d["result_id"] and d["body_hash"] and d["binding_id"] == binding
            for d in exported["documents"]
        ),
    )
    h.cli("run", "a")
    again = h.export()
    h.check("normal_cycle_no_manual_review", len(again["documents"]), 2)
    h.check(
        "repeat_no_new_publications",
        [d["publication_id"] for d in again["documents"]],
        [d["publication_id"] for d in exported["documents"]],
    )
    changed = h.binding(params={"body": "section"})
    h.cli("activate", changed, "--expect-generation", "1", ok=False)
    h.check("changed_parameters_need_review", True)
    h.site.failure = "empty"
    h.cli("run", "a", ok=False)
    h.check("partial_preserves_publication", len(h.export()["documents"]), 2)
    h.site.failure = None
    h.cli("run", "a", ok=False)
    h.check("needs_repair_requires_review", True)
    h.approve(binding)
    h.cli("activate", binding, "--expect-generation", "1")
    h.config("unknown", expected=False)
    unknown = h.binding("unknown")
    h.approve(unknown)
    h.cli("activate", unknown, "--expect-generation", "0")
    h.cli("run", "unknown")
    h.check(
        "unknown_coverage_not_100_percent",
        h.export("unknown")["documents"][0]["coverage"]["status"],
        "unknown",
    )
    return binding, run["run_id"]


def m2(h, binding, previous_run):
    h.site.version = "B"
    queued = h.cli("run", "a", "--enqueue", "--recheck")
    h.start_worker()
    h.wait_worker()
    h.check(
        "real_worker_publishes_recheck",
        {d["body"] for d in h.export()["documents"]},
        {"Public content B"},
    )
    h.site.version = "A"
    h.cli("run", "a", "--recheck")
    history = h.cli("inspect", "source", "a")
    for doc in history["documents"]:
        revisions = [r for r in history["revisions"] if r["document_id"] == doc["id"]]
        h.check(f"a_b_a_history_{doc['identity']}", len(revisions), 3)
        h.check(f"two_body_blobs_{doc['identity']}", len({r["body_hash"] for r in revisions}), 2)
    h.cli("finish", queued["run_id"])
    h.check("finish_transaction_reentrant", True)
    old = h.cli("run", "a", "--enqueue")
    h.cli("pause", "a", "--reason", "synthetic pause")
    h.start_worker()
    h.wait_worker()
    h.check(
        "old_generation_cannot_commit",
        h.cli("inspect", "run", old["run_id"])["run"]["status"],
        "superseded",
    )
    result = h.export()["documents"][0]["result_id"]
    h.cli("withdraw", result, "--reason", "synthetic incorrect extraction")
    export = h.export()
    h.check("withdrawal_exported", len(export["withdrawals"]), 1)
    h.check("withdrawal_clears_pointer", len(export["documents"]), 1)
    h.cli("activate", binding, "--expect-generation", "3")
    h.cli("run", "a", ok=False)
    h.check("withdrawn_result_never_republished", len(h.export()["documents"]), 1)


def m3(h, binding, previous_run):
    recipe = h.cli("inspect", "binding", binding)["recipe_version"]
    h.config("b")
    b = h.binding("b", recipe=recipe)
    h.approve(b)
    h.cli("activate", b, "--expect-generation", "0")
    h.cli("run", "b")
    h.check("shared_initial_recipe", h.cli("inspect", "binding", b)["recipe_version"], recipe)
    before = h.export("b")
    h.site.broken = True
    h.cli("run", "a", ok=False)
    h.cli("run", "b")
    h.check(
        "b_unaffected_by_a_failure",
        [d["result_id"] for d in h.export("b")["documents"]],
        [d["result_id"] for d in before["documents"]],
    )
    fixed = h.root / "fixed-recipe"
    shutil.copytree("recipes/generic", fixed)
    recipe_file = fixed / "recipe.py"
    recipe_file.write_text(
        recipe_file.read_text().replace(
            'params.get("body", "article")', 'params.get("body", "article, section")'
        )
    )
    new_recipe = h.cli("recipe", "pack", fixed)["recipe_version"]
    a2 = h.binding("a", recipe=new_recipe)
    replay = h.cli("replay", previous_run, "--binding", a2)
    trial = h.cli("trial", a2)
    h.approve(a2, trial["run_id"], replay["run_id"])
    h.cli("activate", a2, "--expect-generation", "4")
    h.cli("run", "a")
    h.check("a_fixed_and_published", len(h.export()["documents"]), 2)
    h.check("b_stays_old_version", h.export("b")["documents"][0]["recipe_version"], recipe)
    h.site.broken = False
    h.cli("rollback", binding, "--expect-generation", "5", "--reason", "synthetic rollback")
    h.check("rollback_new_generation", h.cli("inspect", "source", "a")["source"]["generation"], 6)
    h.check("rollback_keeps_withdrawal", len(h.export()["withdrawals"]), 1)
