"""Boundary rejections, immutable input validation and deterministic completion."""

import hashlib
import json
import shutil
import sys


def configuration_and_determinism(h):
    duplicate = h.root / "duplicate.yaml"
    duplicate.write_text("id: a\nid: b\n")
    rejected = h.cli("source", "apply", duplicate, ok=False)
    h.check("duplicate_yaml_rejected", rejected["error"], "duplicate_config_key")
    config = h.config("contract")
    config["HTTPCACHE_ENABLED"] = True
    wrong = h.root / "invalid.json"
    wrong.write_text(json.dumps(config))
    h.cli("source", "apply", wrong, ok=False)
    h.check("production_cache_configuration_rejected", True)
    config.pop("HTTPCACHE_ENABLED")
    config["entry_urls"] = [h.site.url + "/contract/list?access_token=synthetic-test"]
    wrong.write_text(json.dumps(config))
    h.cli("source", "apply", wrong, ok=False)
    h.check("sensitive_source_url_rejected", True)
    bundle = h.root / "contract-recipe"
    shutil.copytree("recipes/generic", bundle)
    (bundle / "escape.py").symlink_to(wrong)
    h.cli("recipe", "pack", bundle, ok=False)
    h.check("recipe_symlink_escape_rejected", True)
    (bundle / "escape.py").unlink()
    (bundle / "helper.py").write_text("VALUE = 1\n")
    first = h.cli("recipe", "pack", bundle)["recipe_version"]
    (bundle / "helper.py").write_text("VALUE = 2\n")
    second = h.cli("recipe", "pack", bundle)["recipe_version"]
    h.check("shared_helper_part_of_version", first != second)
    recipe = bundle / "recipe.py"
    source = recipe.read_text().replace(
        "from io import BytesIO", "from io import BytesIO\nimport os"
    )
    source = source.replace(
        "        yield item",
        "        if os.environ.get('SEAL_E2E_NONDET'):\n            item['attachments'].append({'url': response.url + '/extra', 'status': 'not_fetched'})\n        yield item",
    )
    recipe.write_text(source)
    version = h.cli("recipe", "pack", bundle)["recipe_version"]
    binding = h.binding("contract", recipe=version)
    h.select("contract", binding, 0)
    h.cli("run", "contract")
    original = h.export("contract")
    h.env["SEAL_E2E_NONDET"] = "1"
    try:
        changed = h.cli("run", "contract", ok=False)
        h.check("same_key_different_output_blocked", "nondeterministic_output" in changed["errors"])
        h.check(
            "nondeterminism_does_not_replace_completed_results",
            [d["result_id"] for d in h.export("contract")["documents"]],
            [d["result_id"] for d in original["documents"]],
        )
    finally:
        h.env.pop("SEAL_E2E_NONDET")
    document = original["documents"][0]
    key = document["body_hash"]
    blob = h.root / "archive" / "objects" / key[:2] / key[2:]
    saved = blob.read_bytes()
    blob.write_bytes(b"synthetic corruption")
    try:
        exported = h.export("contract")
        h.check(
            "corrupt_evidence_is_not_exported",
            all(d["document_id"] != document["document_id"] for d in exported["documents"]),
        )
        h.check(
            "corruption_has_explicit_diagnostic",
            any(d["reason"] == "archive_corrupt" for d in exported["unavailable"]),
        )
    finally:
        blob.write_bytes(saved)
    h.check("restored_evidence_hash", hashlib.sha256(blob.read_bytes()).hexdigest(), key)


def runtime_contracts(h):
    from .faults import sql

    h.config(
        "failed",
        entries=[h.site.url + "/partial/one"],
        seed_role="detail",
    )
    binding = h.binding("failed")
    h.select("failed", binding, 0)
    h.cli("run", "failed")
    baseline = h.export("failed")["documents"]
    h.site.version = "B"
    h.config(
        "failed",
        entries=[h.site.url + "/partial/one", h.site.url + "/probe/badpdf"],
        seed_role="detail",
    )
    binding = h.binding("failed")
    failed = h.cli("run", "failed", "--binding", binding, ok=False)
    h.check("partial_has_technical_error", failed["status"], "failed")
    h.check("failed_errors_not_empty", bool(failed["errors"]))
    h.check("failed_errors_in_report", failed["report"]["errors"], failed["errors"])
    h.check(
        "failed_no_duplicate_classification",
        not (
            {"termination", "terminal_reason"}
            & (failed["summary"].keys() | failed["report"].keys())
        ),
    )
    data = h.cli("export", "failed", "--run", failed["run_id"])
    h.check("partial_valid_json_retained", len(data["documents"]), 1)
    h.check("failed_valid_result_is_new_content", data["documents"][0]["body"], "Public content B")
    h.check("partial_not_quality_evaluated", data["quality_status"], "not_evaluated")
    h.check(
        "partial_raw_preserved", len(h.cli("inspect", "run", failed["run_id"])["observations"]) >= 2
    )
    h.check(
        "failed_preserves_success_view",
        [d["result_id"] for d in h.export("failed")["documents"]],
        [d["result_id"] for d in baseline],
    )
    entry = data["documents"][0]
    key = entry["body_hash"]
    blob = h.root / "archive" / "objects" / key[:2] / key[2:]
    original = blob.read_bytes()
    blob.write_bytes(b"synthetic failure evidence corruption")
    try:
        broken = h.cli("export", "failed", "--run", failed["run_id"])
        h.check("failed_export_corrupt_item_rejected", broken["documents"], [])
        h.check(
            "failed_export_corruption_diagnostic",
            broken["unavailable"][0]["reason"],
            "archive_corrupt",
        )
    finally:
        blob.write_bytes(original)
        h.site.version = "A"
    h.cli("export", "a", "--run", failed["run_id"], ok=False)
    h.check("cross_source_export_rejected", True)

    h.config("zero", entries=[h.site.url + "/zero/one"], seed_role="detail")
    folder = h.root / "zero-recipe"
    shutil.copytree("recipes/generic", folder)
    file = folder / "recipe.py"
    file.write_text(file.read_text().replace("        yield item", "        return"))
    version = h.cli("recipe", "pack", folder)["recipe_version"]
    zero = h.cli("run", "zero", "--binding", h.binding("zero", recipe=version))
    h.check("empty_run_is_not_quality_failure", zero["status"], "complete")
    h.check("empty_run_count_explicit", zero["report"]["result_count"], 0)
    h.check("no_quality_coverage_claim", "coverage" not in zero["report"])

    h.config("queued_candidate")
    binding = h.binding("queued_candidate")
    queued = h.cli("run", "queued_candidate", "--binding", binding, "--enqueue")
    h.start_worker()
    h.wait_worker()
    result = h.cli("inspect", "run", queued["run_id"])
    h.check("queued_candidate_without_default_complete", result["run"]["status"], "complete")
    h.check("queued_candidate_json", len(result["results"]), 2)
    h.check(
        "no_v11_publication_records",
        sql(
            h, "SELECT count(*) FROM seal_decision WHERE kind!='control' AND id NOT LIKE 'legacy-%'"
        )[0][0],
        0,
    )


def stopped_source_replay(h):
    history = h.cli("inspect", "source", "b")
    run = next(
        r["id"]
        for r in reversed(history["runs"])
        if r["mode"] == "collect" and r["status"] == "complete"
    )
    before = len(h.site.ledger)
    h.site.close()
    replay = h.cli("replay", run)
    h.check("stopped_source_replay_succeeds", replay["status"], "complete")
    h.check("stopped_source_replay_zero_network", len(h.site.ledger), before)


def historical_bundle_compatibility(h):
    """Freeze a synthetic prior engine fingerprint; never relax environment checks."""
    from .faults import sql

    h.config("compatibility")
    binding = h.binding("compatibility")
    original = h.cli("run", "compatibility", "--binding", binding)
    original_data = h.cli("export", "compatibility", "--run", original["run_id"])
    version = h.cli("inspect", "binding", binding)["recipe_version"]
    objects = h.root / "archive" / "objects"
    bundle = json.loads((objects / version[:2] / version[2:]).read_bytes())
    bundle["environment"]["engine"]["runs.py"] = "synthetic-prior-runtime-fingerprint"
    raw = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    prior = hashlib.sha256(raw).hexdigest()
    target = objects / prior[:2] / prior[2:]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    sql(
        h,
        "INSERT INTO seal_recipe_version(id,manifest) VALUES(%s,%s::jsonb)",
        (
            prior,
            json.dumps(
                {
                    "object": prior,
                    "environment": bundle["environment"],
                    "python": sys.executable,
                    "family": bundle["manifest"]["family"],
                }
            ),
        ),
    )
    sql(
        h,
        "INSERT INTO seal_binding(id,source_id,recipe_version,config,params,fingerprint) SELECT 'prior-binding',source_id,%s,config,params,'prior-binding' FROM seal_binding WHERE id=%s",
        (prior, binding),
    )
    before = len(h.site.ledger)
    rejected = h.cli("replay", original["run_id"], "--binding", "prior-binding", ok=False)
    h.check("old_binding_rejected_by_current_runtime", rejected["status"], "failed")
    h.check("old_binding_environment_error", "environment_drift" in rejected["errors"])
    h.check("old_binding_no_network", len(h.site.ledger), before)
    h.check("old_archive_bytes_unchanged", target.read_bytes(), raw)
    h.check(
        "old_results_readable_after_rejected_replay",
        len(h.cli("export", "compatibility", "--run", original["run_id"])["documents"]),
        len(original_data["documents"]),
    )
    refreshed = h.binding("compatibility")
    replay = h.cli("replay", original["run_id"], "--binding", refreshed)
    replay_data = h.cli("export", "compatibility", "--run", replay["run_id"])
    h.check("repacked_binding_replay_complete", replay["status"], "complete")
    h.check("repacked_binding_replay_no_network", len(h.site.ledger), before)
    for field in ("title", "body", "snapshot_ids", "locators"):
        h.check(
            "repacked_replay_preserves_" + field,
            [d.get(field) for d in replay_data["documents"]],
            [d.get(field) for d in original_data["documents"]],
        )
