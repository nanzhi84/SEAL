"""External acceptance contracts for declared resources, lineage and seeds."""

import json

from live_verify import canonical_url, object_bytes


def resources(h, sample, label, inspected):
    prefix = sample["source"]["id"] + "_" + label
    expected, run = sample["expected"], inspected["run"]
    h.check(
        prefix + "_only_expected_errors",
        sorted(run["report"]["errors"]),
        sorted(expected.get("errors", [])),
    )
    events = inspected["discovery"]["events"]
    h.check(prefix + "_no_pending_discoveries", inspected["discovery"]["pending"], 0)
    required = (
        expected.get("archived_urls", [])
        if label != "recheck"
        else [seed["url"] for seed in run["seeds"]]
    )
    archived = set()
    for event in events:
        if event["snapshot_id"]:
            snapshot = json.loads(object_bytes(h.root / "archive", event["snapshot_id"]))
            if snapshot["method"] == "GET" and snapshot["status"] == 200:
                archived.add(canonical_url(snapshot["url"]))
    h.check(
        prefix + "_all_declared_inputs_archived",
        {canonical_url(url) for url in required} <= archived,
    )


def attachment_lineage(h, sample, label, inspected, exported):
    prefix = sample["source"]["id"] + "_" + label
    for event in inspected["discovery"]["events"]:
        if event["role"] != "attachment" or not event["snapshot_id"]:
            continue
        records = [
            record
            for record in exported["records"]
            if any(ref["snapshot_id"] == event["snapshot_id"] for ref in record["inputs"])
        ]
        if event["reason"] in ("unsupported_content_type", "pdf_text_layer_required"):
            h.check(prefix + "_unsupported_not_fabricated_" + event["id"], records, [])
            continue
        h.check(prefix + "_attachment_has_business_output_" + event["id"], bool(records))
        if label != "recheck":
            pair = (event["parent_snapshot_id"], event["parent_observation_id"])
            h.check(prefix + "_parent_discovery_evidence_" + event["id"], all(pair))
            h.check(
                prefix + "_parent_in_record_inputs_" + event["id"],
                all(
                    pair
                    in {(ref["snapshot_id"], ref["observation_id"]) for ref in record["inputs"]}
                    for record in records
                ),
            )


def seed_contract(h, source, previous, inspected):
    expected = set()
    for record in previous["records"]:
        if record["detail_url"]:
            expected.add((record["detail_url"], "detail", "GET"))
        else:
            parent = record["frozen_parent_request"]
            expected.add((parent["url"], parent["role"], parent["method"]))
    actual = {
        (seed["url"], seed["role"], seed.get("method", "GET")) for seed in inspected["run"]["seeds"]
    }
    h.check(source + "_recheck_exact_frozen_seeds", sorted(actual), sorted(expected))


def replay_inputs(h, source, original, replayed):
    before = {item["snapshot_id"] for item in original["run"]["inputs"]}
    after = {item["snapshot_id"] for item in replayed["run"]["inputs"]}
    h.check(source + "_replay_exact_original_inputs", sorted(after), sorted(before))
