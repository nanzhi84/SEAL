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
    if "http_attempts" in expected:
        h.check(
            prefix + "_bounded_http_attempts",
            inspected["discovery"]["http_attempts"],
            expected["http_attempts"],
        )
    if "http_statuses" in expected:
        h.check(
            prefix + "_observed_http_statuses",
            [row["status"] for row in inspected["observations"]],
            expected["http_statuses"],
        )
    if "blocked_redirect" in expected:
        blocked = [event for event in events if event["url"] == expected["blocked_redirect"]]
        h.check(prefix + "_blocked_redirect_present", len(blocked), 1)
        h.check(prefix + "_blocked_redirect_not_requested", blocked[0]["requested_at"], None)
        h.check(prefix + "_blocked_redirect_reason", blocked[0]["reason"], "request_out_of_scope")
        redirects = [row for row in inspected["observations"] if row["status"] == 302]
        h.check(prefix + "_redirect_raw_preserved", len(redirects), 1)
        snapshot = json.loads(object_bytes(h.root / "archive", redirects[0]["snapshot_id"]))
        object_bytes(h.root / "archive", snapshot["body_hash"])
        locations = [
            value
            for key, values in snapshot["headers"].items()
            if key.lower() == "location"
            for value in values
        ]
        h.check(prefix + "_raw_redirect_location", locations, [expected["blocked_redirect"]])
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


def document_contracts(h, sample, label, exported):
    """Assert every declared attachment's business text and retained topology.

    An anchor in a notice or another attachment cannot satisfy this contract.
    Runtime-independent field recomputation is still required by verify_export.
    These are fixed public document expectations, not entity row identities.
    """
    prefix = sample["source"]["id"] + "_" + label + "_document_"
    for index, expected in enumerate(sample["expected"].get("document_contracts", [])):
        tag = prefix + str(index)
        matches = [
            record
            for record in exported["records"]
            if canonical_url(record["record_key"]) == canonical_url(expected["record_key"])
        ]
        h.check(tag + "_exact_record", len(matches), 1)
        record = matches[0]
        data = record["data"]
        for field in ("title", "body"):
            value = data.get(field)
            h.check(tag + "_" + field + "_present", isinstance(value, str) and bool(value))
            for anchor in expected.get(field + "_anchors", []):
                h.check(tag + "_" + field + "_anchor_" + anchor, anchor in value)
        h.check(tag + "_body_minimum", len(data["body"]) >= expected["min_body_chars"])
        primary_ids = {item["primary_snapshot_id"] for item in record["result_evidence"]}
        primary_hashes = {
            ref["body_hash"] for ref in record["inputs"] if ref["snapshot_id"] in primary_ids
        }
        h.check(tag + "_immutable_raw_digest", primary_hashes, {expected["archive_sha256"]})
        if "worksheets" in expected:
            sheets = data.get("worksheets", [])
            h.check(tag + "_worksheet_count", len(sheets), len(expected["worksheets"]))
            for sheet, contract in zip(sheets, expected["worksheets"], strict=True):
                h.check(tag + "_worksheet_name", sheet["name"], contract["name"])
                h.check(tag + "_effective_rows", len(sheet["rows"]), contract["rows"])
                h.check(
                    tag + "_effective_columns",
                    sorted({len(row) for row in sheet["rows"]}),
                    [contract["columns"]],
                )
                h.check(tag + "_merged_cells", len(sheet["merged_cells"]), contract["merged_cells"])
                for cell in contract.get("cells", []):
                    h.check(
                        tag + "_cell_" + str(cell["row"]) + "_" + str(cell["column"]),
                        sheet["rows"][cell["row"]][cell["column"]],
                        cell["value"],
                    )
                if "business_codes" in contract:
                    code_contract = contract["business_codes"]
                    values = [row[code_contract["column"]] for row in sheet["rows"]]
                    # The actual source's 10-digit school identifiers are
                    # verified as business content, never inferred from row order.
                    codes = [
                        str(int(value))
                        if type(value) is float and value.is_integer()
                        else str(value)
                        for value in values
                    ]
                    codes = [code for code in codes if len(code) == 10 and code.isdecimal()]
                    h.check(tag + "_school_code_count", len(codes), code_contract["count"])
                    h.check(tag + "_school_code_unique", len(set(codes)), len(codes))
        if "blocks" in expected:
            blocks = data.get("blocks", [])
            tables = [block for block in blocks if block.get("kind") == "table"]
            h.check(tag + "_ordered_block_count", len(blocks), expected["blocks"]["count"])
            h.check(
                tag + "_table_rows",
                [len(table["rows"]) for table in tables],
                expected["blocks"]["table_rows"],
            )
            h.check(
                tag + "_table_columns",
                [table["columns"] for table in tables],
                expected["blocks"]["table_columns"],
            )
            cells = [cell for table in tables for row in table["rows"] for cell in row["cells"]]
            for key, actual in (
                ("merged_cells", sum(cell.get("span", 1) > 1 for cell in cells)),
                ("vertical_merges", sum("vertical_merge" in cell for cell in cells)),
            ):
                h.check(tag + "_" + key, actual, expected["blocks"][key])
        if "coverage" in expected:
            coverage = data.get("coverage", {})
            h.check(
                tag + "_explicit_unparsed_elements",
                coverage.get("unparsed_elements"),
                expected["coverage"]["unparsed_elements"],
            )
            h.check(
                tag + "_explicit_non_body_parts",
                [part["part"] for part in coverage.get("non_body_parts", [])],
                expected["coverage"]["non_body_parts"],
            )
