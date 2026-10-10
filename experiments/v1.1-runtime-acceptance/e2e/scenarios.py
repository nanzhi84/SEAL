import shutil


def m0(h):
    endpoints = ["redirect", "meta", "retry", "deflate", "pdf", "json", "txt"]
    h.config("probe", [h.site.url + "/probe/" + p for p in endpoints], seed_role="detail")
    binding = h.binding("probe")
    run_result = h.cli("run", "probe", "--binding", binding)
    h.check("native_crawl_complete", run_result["status"], "complete")
    run = h.cli("inspect", "run", run_result["run_id"])
    statuses = [o["status"] for o in run["observations"]]
    h.check("every_retry_archived", statuses.count(503), 2)
    h.check("redirect_archived", 302 in statuses)
    h.check(
        "pdf_text_matches_oracle",
        any("Synthetic PDF notice" in r["candidate"]["body"] for r in run["results"]),
    )
    h.check("callback_inputs_durable", all(r["checks"]["input_verified"] for r in run["results"]))
    before = len(h.site.ledger)
    replay = h.cli("replay", run_result["run_id"])
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
        name = "bad_" + endpoint
        h.config(name, [h.site.url + "/probe/" + endpoint], seed_role="detail")
        failed = h.cli("run", name, "--binding", h.binding(name), ok=False)
        h.check(f"reject_{endpoint}", failed["status"] in ("partial", "failed", "retryable"))
    return binding


def m1(h):
    h.config()
    binding = h.binding()
    run = h.cli("run", "a", "--binding", binding)
    h.check("candidate_without_default_runs", run["status"], "complete")
    source = h.cli("inspect", "source", "a")
    h.check("run_does_not_select_default", source["source"]["binding_id"], None)
    h.check(
        "no_governance_or_publication_events", {d["kind"] for d in source["decisions"]}, {"control"}
    )
    exported = h.export()
    h.check(
        "runtime_expected_set",
        sorted(d["title"] for d in exported["documents"]),
        ["First notice", "Second notice"],
    )
    h.check(
        "runtime_traceability",
        all(
            d["revision_id"]
            and d["result_id"]
            and d["body_hash"]
            and d["observation_id"]
            and d["run_id"] == run["run_id"]
            and d["binding_id"] == binding
            for d in exported["documents"]
        ),
    )
    h.check("results_not_quality_approved", exported["quality_status"], "not_evaluated")
    h.select("a", binding, 0)
    rejected = h.cli(
        "source", "select", "a", "--binding", binding, "--expect-generation", "0", ok=False
    )
    h.check("default_selection_cas", rejected["error"], "generation_conflict")
    h.cli("run", "a")
    again = h.export()
    h.check("normal_cycle_complete", len(again["documents"]), 2)
    h.check(
        "repeat_reuses_results",
        [d["result_id"] for d in again["documents"]],
        [d["result_id"] for d in exported["documents"]],
    )
    h.check("repeat_no_new_revisions", len(h.cli("inspect", "source", "a")["revisions"]), 2)
    changed = h.binding(params={"body": "article", "title": "h1"})
    candidate = h.cli("run", "a", "--binding", changed)
    h.check("non_default_binding_same_runtime", candidate["status"], "complete")
    h.check(
        "same_content_new_binding_no_revision", len(h.cli("inspect", "source", "a")["revisions"]), 2
    )
    h.check(
        "new_binding_new_processing_result",
        {d["result_id"] for d in h.export()["documents"]}.isdisjoint(
            d["result_id"] for d in again["documents"]
        ),
    )
    h.check(
        "candidate_does_not_change_default",
        h.cli("inspect", "source", "a")["source"]["binding_id"],
        binding,
    )
    h.site.failure = "empty"
    h.cli("run", "a", ok=False)
    h.check("partial_preserves_successful_view", len(h.export()["documents"]), 2)
    h.site.failure = None
    h.check("failure_needs_no_reapproval", h.cli("run", "a")["status"], "complete")
    h.config("other")
    rejected = h.cli("run", "other", "--binding", binding, ok=False)
    h.check("cross_source_binding_rejected", rejected["error"], "binding_source_mismatch")
    return binding, run["run_id"]


def m2(h, binding, previous_run):
    h.site.version = "B"
    queued = h.cli("run", "a", "--enqueue", "--recheck")
    h.start_worker()
    h.wait_worker()
    h.check(
        "real_worker_rechecks", {d["body"] for d in h.export()["documents"]}, {"Public content B"}
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
    h.select("a", binding, 2)
    historical = h.cli("export", "a", "--run", previous_run)
    h.check(
        "historical_run_revision_stable",
        {d["body"] for d in historical["documents"]},
        {"Public content A"},
    )
    h.check(
        "historical_run_ids_stable", {d["run_id"] for d in historical["documents"]}, {previous_run}
    )


def m3(h, binding, previous_run):
    recipe = h.cli("inspect", "binding", binding)["recipe_version"]
    h.config("b")
    b = h.binding("b", recipe=recipe)
    h.select("b", b, 0)
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
    h.cli("replay", previous_run, "--binding", a2)
    h.cli("run", "a", "--binding", a2)
    h.check("candidate_repair_results_saved", len(h.export()["documents"]), 2)
    h.select("a", a2, 3)
    h.check("b_stays_old_version", h.export("b")["documents"][0]["recipe_version"], recipe)
    h.site.broken = False
    h.select("a", binding, 4)
    h.check(
        "select_previous_version_new_generation",
        h.cli("inspect", "source", "a")["source"]["generation"],
        5,
    )
    h.cli("run", "a")
