"""Sensitive response acceptance through the existing real Runtime CLI harness."""

import hashlib
import json

from lxml import html
from runtime_support import archived, by_key, run
from sensitive_fixtures import REJECTED_BODIES, SAFE_BODY


def sensitive_responses(h, version):
    h.config(
        "sensitivebaseline",
        entries=[h.site.url + "/sensitive/baseline"],
        identity="canonical_url",
        output_schema="record.v1",
        seed_role="detail",
        delay=0.0,
    )
    binding = h.binding("sensitivebaseline", recipe=version, params={"mode": "assets"})
    receipt, inspected, exported = run(
        h, "sensitivebaseline", binding, capture="sensitive-public-placeholders"
    )
    original = by_key(exported)
    h.check("sensitive_placeholders_complete", receipt["status"], "complete")
    h.check("sensitive_placeholders_real_record", len(original), 1)
    snapshot = json.loads(archived(h, inspected["observations"][0]["snapshot_id"]))
    raw = archived(h, snapshot["body_hash"])
    h.check("sensitive_placeholders_raw_unchanged", raw == SAFE_BODY)
    parsed = html.fromstring(raw)
    for result in inspected["record_results"]:
        candidate = result["candidate"]
        for field, value in candidate["data"].items():
            node = parsed.xpath(candidate["locators"][field]["path"])[0]
            actual = " ".join(node.xpath(".//text() | self::text()")).strip()
            h.check("sensitive_independent_locator_" + field, actual, value)
    _, _, rechecked = run(h, "sensitivebaseline", binding, recheck=True)
    h.check("sensitive_recheck_identity_stable", set(by_key(rechecked)), set(original))
    h.check(
        "sensitive_recheck_version_stable",
        [record["record_version_id"] for record in by_key(rechecked).values()],
        [record["record_version_id"] for record in original.values()],
    )
    before = len(h.site.ledger)
    replay = h.cli("replay", receipt["run_id"])
    h.check("sensitive_replay_zero_http", len(h.site.ledger), before)
    h.check("sensitive_replay_complete", replay["status"], "complete")
    replayed = h.cli("export", "sensitivebaseline", "--run", replay["run_id"])
    h.check("sensitive_replay_identity_stable", set(by_key(replayed)), set(original))
    baseline = h.export("sensitivebaseline")
    h.site.state = "sensitive"
    try:
        before = len(h.site.ledger)
        rejected, observed, data = run(
            h, "sensitivebaseline", binding, ok=False, capture="sensitive-changed-response"
        )
        h.check("sensitive_body_error_traced", "sensitive_body_rejected" in rejected["errors"])
        h.check("sensitive_changed_no_observation", observed["observations"], [])
        h.check(
            "sensitive_rejected_response_still_counts_network_attempt",
            [
                rejected["report"]["stats"].get("downloader/request_count", 0),
                rejected["report"]["discovery"]["http_attempts"],
                rejected["report"]["resource_counts"]["http_attempts"],
                observed["discovery"]["http_attempts"],
                observed["manifest"]["resource_counts"]["http_attempts"],
                len(h.site.ledger) - before,
            ],
            [1, 1, 1, 1, 1, 1],
        )
        h.check("sensitive_changed_no_record", data["records"], [])
        preserved = h.export("sensitivebaseline")

        def immutable_outputs(value):
            return {
                record["record_key"]: {
                    field: record[field]
                    for field in ("record_id", "record_version_id", "record_result_id", "data")
                }
                for record in value["records"]
            }

        h.check(
            "sensitive_failure_preserves_success",
            immutable_outputs(preserved),
            immutable_outputs(baseline),
        )
        h.check(
            "sensitive_failure_retained_success_marked_stale",
            all(record["stale"] for record in preserved["records"]),
        )
    finally:
        h.site.state = "base"
    for name, raw in REJECTED_BODIES.items():
        source = "sensitivereject" + name
        h.config(
            source,
            entries=[h.site.url + "/sensitive/" + name],
            identity="canonical_url",
            output_schema="record.v1",
            seed_role="detail",
            delay=0.0,
        )
        target = h.binding(source, recipe=version, params={"mode": "assets"})
        failed, rejected_inspect, data = run(
            h, source, target, ok=False, capture="sensitive-rejected-" + name
        )
        h.check("sensitive_" + name + "_diagnostic", "sensitive_body_rejected" in failed["errors"])
        h.check(
            "sensitive_" + name + "_discovery_failed", rejected_inspect["discovery"]["failed"], 1
        )
        h.check("sensitive_" + name + "_no_observation", rejected_inspect["observations"], [])
        h.check("sensitive_" + name + "_no_emission", data["records"], [])
        raw_hash = hashlib.sha256(raw).hexdigest()
        h.check(
            "sensitive_" + name + "_no_raw_archive",
            not (h.root / "archive/objects" / raw_hash[:2] / raw_hash[2:]).exists(),
        )
    h.check(
        "sensitive_receipts_contain_no_credentials",
        "synthetic-private-value" not in json.dumps(h.receipts),
    )
