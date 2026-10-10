"""Bounded public Source acceptance through CLI, isolated PG and real Scrapy.

Acceptance expectations are fixed in live_samples/*.json before adapters run:
wrong/missing business fields, incomplete lineage, invented keys, unarchived
resources, unexpected partial, version churn and network Replay must fail.
Unsupported attachments must retain bytes and explicit partial diagnostics.
No production DSN, authentication, cookies, browser or OCR is used.
"""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from acceptance import Harness as JointHarness
from live_contracts import (
    attachment_lineage,
    document_contracts,
    replay_inputs,
    resources,
    seed_contract,
)
from live_verify import verify_export

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


class Harness(JointHarness):
    def __init__(self, output):
        super().__init__(output)
        # Keep the fixture lifecycle reusable; it is never an accepted Source.
        self.env.pop("SEAL_ALLOW_LOOPBACK", None)
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            self.env.pop(name, None)
        self.samples = []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.command = (
            "uv run --frozen python experiments/v1.2-acceptance/live.py --output <new-directory>"
        )
        self.input_samples = {}

    def cli(self, *args, ok=True):
        # Unlike the synthetic helper, expected partials are successful checks
        # of diagnostic behavior. Still distinguish CLI errors from run status.
        process = subprocess.run(
            [sys.executable, "-m", "seal", *map(str, args)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        try:
            data = json.loads(process.stdout)
        except ValueError:
            data = {"error": "invalid_cli_json"}
        self.receipts.append(
            {"args": list(map(str, args)), "exit": process.returncode, "result": data}
        )
        if ok and process.returncode not in (0, 1):
            raise AssertionError(f"CLI failed: {args[:2]} {data.get('error')}")
        return data

    def save_live(self, error):
        self.site.ledger = [
            observation
            for receipt in self.receipts
            if receipt["args"][:2] == ["inspect", "run"]
            for observation in receipt["result"].get("observations", [])
        ]
        super().save("real-sources", error)
        path = self.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(
            stage="real-sources",
            command=self.command,
            input_samples=self.input_samples,
            scope="Bounded real public A/B/C sources; isolated PostgreSQL; no quality approval",
            started_at=self.started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            environment={
                "python": sys.version,
                "postgresql": subprocess.check_output(["pg_ctl", "--version"], text=True).strip(),
                "network": "direct public GET; no cookies/auth/environment proxy",
            },
            real_source_acceptance={
                "status": "PASS" if error is None else "FAIL",
                "samples": self.samples,
            },
            unverified=[
                "Whole-site completeness",
                "Formal Evaluation and business quality approval",
                "OCR, XLSX and unsupported embedded document content",
                "Power loss durability",
            ],
        )
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        passed = sum(item["status"] == "PASS" for item in self.assertions)
        self.output.joinpath("report.txt").write_text(
            f"Real Sources: {passed}/{len(self.assertions)} PASS\n"
            + json.dumps(self.samples, ensure_ascii=False, indent=2)
            + "\n"
        )


def capture(h, name, data):
    h.capture(name, data)


def run(h, sample, binding, label, *, recheck=False, replay=None):
    source = sample["source"]["id"]
    args = ["replay", replay] if replay else ["run", source, "--binding", binding]
    if recheck:
        args.append("--recheck")
    receipt = h.cli(*args)
    if "run_id" not in receipt:
        capture(h, f"{source}-{label}", {"receipt": receipt})
        return receipt, None, None
    inspected = h.cli("inspect", "run", receipt["run_id"])
    exported = h.cli("export", source, "--run", receipt["run_id"])
    evidence = {"receipt": receipt, "inspect": inspected, "export": exported}
    capture(h, f"{source}-{label}", evidence)
    prefix = source + "_" + label
    h.check(prefix + "_status", receipt["status"] in sample["expected"]["statuses"])
    h.check(
        prefix + "_status_consistent",
        [inspected["run"]["status"], exported["run"]["status"]],
        [receipt["status"]] * 2,
    )
    h.check(prefix + "_quality", exported["quality_status"], "not_evaluated")
    h.check(prefix + "_manifest", inspected["availability"], "available")
    h.check(
        prefix + "_research_mapping",
        inspected["manifest"]["research_ids"],
        sample["source"]["research_ids"],
    )
    h.check(
        prefix + "_has_business_results",
        len(exported["records"]) >= sample["expected"]["min_records"],
    )
    for field in ("title", "body"):
        values = [record["data"].get(field) for record in exported["records"]]
        for anchor in sample["expected"].get(field + "_anchors", []):
            h.check(
                prefix + "_" + field + "_anchor_" + anchor,
                any(isinstance(value, str) and anchor in value for value in values),
            )
    if "min_body_chars" in sample["expected"]:
        h.check(
            prefix + "_business_body_minimum",
            all(
                isinstance(record["data"].get("body"), str)
                and len(record["data"]["body"]) >= sample["expected"]["min_body_chars"]
                for record in exported["records"]
            ),
        )
    h.check(prefix + "_evidence_recomputable", verify_export(h.root / "archive", exported), [])
    resources(h, sample, label, inspected)
    attachment_lineage(h, sample, label, inspected, exported)
    document_contracts(h, sample, label, exported)
    if "record_count" in sample["expected"]:
        h.check(
            prefix + "_exact_business_count",
            len(exported["records"]),
            sample["expected"]["record_count"],
        )
    if "allowed_urls" in sample["expected"]:
        urls = {event["url"] for event in inspected["discovery"]["events"]}
        h.check(
            prefix + "_declared_sample_scope",
            urls <= set(sample["expected"]["allowed_urls"]),
        )
    if replay:
        h.check(prefix + "_no_network_observations", inspected["observations"], [])
        stats = receipt["report"].get("stats", {})
        h.check(prefix + "_no_downloader_network", stats.get("downloader/request_count", 0), 0)
        h.check(prefix + "_no_network_http_attempts", inspected["discovery"]["http_attempts"], 0)
    return receipt, inspected, exported


def business(export):
    return {(r["record_type"], r["record_key"]): r for r in export["records"]}


def compare(h, source, first, second, label, *, replay=False):
    previous, current = business(first), business(second)
    shared = sorted(previous.keys() & current.keys())
    h.check(source + label + "_shared_business_keys", bool(shared))
    unchanged = [key for key in shared if previous[key]["data"] == current[key]["data"]]
    if not replay:
        h.check(
            source + label + "_identity_stable",
            all(previous[key]["record_id"] == current[key]["record_id"] for key in shared),
        )
        h.check(
            source + label + "_same_data_same_content",
            all(previous[key]["content_hash"] == current[key]["content_hash"] for key in unchanged),
        )
        if first["run"]["status"] == second["run"]["status"] == "complete":
            h.check(
                source + label + "_same_data_same_version",
                all(
                    previous[key]["record_version_id"] == current[key]["record_version_id"]
                    for key in unchanged
                ),
            )
            h.check(
                source + label + "_changed_data_changes_own_version",
                all(
                    previous[key]["record_version_id"] != current[key]["record_version_id"]
                    for key in shared
                    if key not in unchanged
                ),
            )
    else:
        h.check(
            source + label + "_original_business_values",
            {str(k): v["data"] for k, v in current.items()},
            {str(k): v["data"] for k, v in previous.items()},
        )


def accept(h, sample, *, diagnostic_only=False):
    config, expected = sample["source"], sample["expected"]
    source = config["id"]
    path = h.root / (source + ".json")
    path.write_text(json.dumps(config, ensure_ascii=False))
    h.cli("source", "apply", path)
    version = h.cli("recipe", "pack", sample["recipe"])["recipe_version"]
    binding = h.binding(source, recipe=version, params=sample["params"])
    mapping = {
        "research_id": sample["research_id"],
        "class": sample["class"],
        "source_id": source,
        "binding_id": binding,
        "recipe_version": version,
        "scope": config["scope"],
        "runs": [],
        "status": "RUNNING",
    }
    h.samples.append(mapping)
    capture(h, source + "-configuration", sample)
    first, inspect_first, initial = run(h, sample, binding, "first")
    mapping["runs"].append(first["run_id"])
    if diagnostic_only:
        # A refused public resource gets one bounded Run, not four repeated
        # attempts. Its diagnostic evidence never counts as adapted/partial
        # business acceptance and is not eligible for Recheck or Replay.
        h.check(source + "_diagnostic_only_not_complete", first["status"] != "complete")
        h.check(source + "_diagnostic_only_no_business_output", initial["records"], [])
        mapping.update(
            status="DIAGNOSTIC_PASS",
            runtime_status=first["status"],
            record_count=0,
            errors=first["report"]["errors"],
            verification_scope="single_run_diagnostic_only",
        )
        return
    text = json.dumps([r["data"] for r in initial["records"]], ensure_ascii=False)
    for anchor in expected.get("anchors", []):
        h.check(source + "_business_anchor_" + anchor, anchor in text)
    events = inspect_first["discovery"]["events"]
    roles = {event["role"] for event in events if event["archived_at"]}
    h.check(source + "_real_resource_roles", set(expected.get("roles", [])) <= roles)
    h.check(
        source + "_partial_explicit_errors",
        set(expected.get("errors", [])) <= set(first["report"]["errors"]),
    )
    pages = {
        event["url"]
        for event in events
        if event["archived_at"] and event["role"] in ("list", "api")
    }
    h.check(source + "_pagination", len(pages) >= expected.get("min_pages", 1))
    if expected.get("no_detail_url"):
        h.check(
            source + "_no_invented_details",
            all(r["detail_url"] is None for r in initial["records"]),
        )
    if expected.get("stable_keys"):
        h.check(
            source + "_known_business_keys",
            set(expected["stable_keys"]) <= {r["record_key"] for r in initial["records"]},
        )
    # Real site changes are evidence, never manufactured source revisions.
    second, _, current = run(h, sample, binding, "second")
    mapping["runs"].append(second["run_id"])
    compare(h, source, initial, current, "_two_rounds")
    if second["status"] == "complete":
        checked, inspected, rechecked = run(h, sample, binding, "recheck", recheck=True)
        mapping["runs"].append(checked["run_id"])
        seeds = inspected["run"]["seeds"]
        h.check(
            source + "_recheck_unique_parent_requests", len({s["url"] for s in seeds}), len(seeds)
        )
        compare(h, source, current, rechecked, "_recheck")
        seed_contract(h, source, current, inspected)
    else:
        blocked = h.cli("run", source, "--binding", binding, "--recheck", ok=False)
        h.check(
            source + "_partial_not_promoted_for_recheck",
            blocked.get("error"),
            "no_documents_to_recheck",
        )
        capture(h, source + "-recheck", blocked)
        h.check(source + "_partial_not_in_default_export", h.cli("export", source)["records"], [])
        mapping["recheck"] = "not_promoted: expected partial"
    replayed, inspected_replay, replay_export = run(
        h, sample, binding, "replay", replay=first["run_id"]
    )
    replay_inputs(h, source, inspect_first, inspected_replay)
    mapping["runs"].append(replayed["run_id"])
    compare(h, source, initial, replay_export, "_replay", replay=True)
    mapping.update(
        status="PASS",
        runtime_status=first["status"],
        record_count=len(initial["records"]),
        record_ids=[r["record_id"] for r in initial["records"]],
        errors=first["report"]["errors"],
    )
    print(
        json.dumps(
            {
                "source": source,
                "status": "PASS",
                "runtime_status": first["status"],
                "records": len(initial["records"]),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", action="append")
    parser.add_argument(
        "--diagnostic-only",
        action="store_true",
        help="One bounded refused-resource Run; never counts as Source adaptation",
    )
    parser.add_argument(
        "--samples", type=Path, action="append", help="Explicit frozen Source acceptance contracts"
    )
    args = parser.parse_args()
    os.chdir(ROOT)
    output = args.output
    if output.exists():
        output = output / datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")
    sample_paths = args.samples or sorted((HERE / "live_samples").glob("[abc].json"))
    samples = [sample for path in sample_paths for sample in json.loads(path.read_text())]
    if args.source:
        samples = [
            sample
            for sample in samples
            if sample["research_id"] in args.source or sample["source"]["id"] in args.source
        ]
    if not samples:
        raise SystemExit("No configured real Sources")
    h, errors = Harness(output), []
    h.command = shlex.join(
        ["uv", "run", "--frozen", "python", "experiments/v1.2-acceptance/live.py", *sys.argv[1:]]
    )
    h.input_samples = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sample_paths
    }
    try:
        h.start()
        if any(sample["research_id"] == "dd-247" for sample in samples):
            route_path = h.output / "c-route-experiment.json"
            subprocess.run(
                [
                    sys.executable,
                    str(HERE / "live_samples/c_route_experiment.py"),
                    "--output",
                    str(route_path),
                ],
                env=h.env,
                check=True,
                capture_output=True,
                text=True,
            )
            h.check(
                "T0_real_api_routes", json.loads(route_path.read_text())["assertions"]["failed"], 0
            )
        for sample in samples:
            try:
                accept(h, sample, diagnostic_only=args.diagnostic_only)
            except Exception as exc:
                errors.append(f"{sample['research_id']}: {type(exc).__name__}: {exc}")
                if h.samples and h.samples[-1]["research_id"] == sample["research_id"]:
                    h.samples[-1]["status"] = "FAIL"
                print(errors[-1], flush=True)
                traceback.print_exc()
        if args.samples:
            h.check(
                "selected_sample_set_processed",
                sorted(
                    s["research_id"]
                    for s in h.samples
                    if s["status"] == ("DIAGNOSTIC_PASS" if args.diagnostic_only else "PASS")
                ),
                sorted(s["research_id"] for s in samples),
            )
        elif not args.source:
            counts = Counter(
                sample["class"]
                for sample in h.samples
                if sample["status"] == "PASS" and sample.get("runtime_status") == "complete"
            )
            h.check(
                "T7S_each_class_two_real_sources", all(counts[category] >= 2 for category in "ABC")
            )
    except Exception as exc:
        errors.append(str(exc))
    finally:
        h.save_live("; ".join(errors) if errors else None)
        h.close()
    print(output.resolve(), flush=True)
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
