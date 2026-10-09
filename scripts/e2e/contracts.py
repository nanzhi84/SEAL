"""Boundary rejections, immutable input validation and deterministic publication."""

import hashlib
import json
import shutil


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
    h.approve(binding)
    h.cli("activate", binding, "--expect-generation", "0")
    h.cli("run", "contract")
    original = h.export("contract")
    h.env["SEAL_E2E_NONDET"] = "1"
    try:
        changed = h.cli("run", "contract", ok=False)
        h.check("same_key_different_output_blocked", "nondeterministic_output" in changed["errors"])
        h.check(
            "nondeterminism_does_not_replace_published_results",
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


def stopped_source_replay(h):
    history = h.cli("inspect", "source", "b")
    run = next(
        r["id"]
        for r in reversed(history["runs"])
        if r["mode"] == "production" and r["status"] == "complete"
    )
    before = len(h.site.ledger)
    h.site.close()
    replay = h.cli("replay", run)
    h.check("stopped_source_replay_succeeds", replay["status"], "complete")
    h.check("stopped_source_replay_zero_network", len(h.site.ledger), before)
