"""Real CLI regression: inherited broken proxy must not change Source behavior.

Predeclared failure modes: collect contacts a refused local environment proxy
instead of the permitted Source; ten business records/raw bytes are missing;
Replay has missing inputs or performs network access. The same isolated Source
must produce complete business data and offline Replay regardless of proxies.
No setting-value assertion, authenticated proxy, or public website is involved.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from acceptance import Harness
from live_verify import verify_export
from test_runtime import recipe, record_source

ROOT = Path(__file__).resolve().parents[2]
DUMMY_PROXY = "http://127.0.0.1:1"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def command(h, *args):
    process = subprocess.run(
        [sys.executable, "-m", "seal", *map(str, args)],
        env=h.env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    try:
        result = json.loads(process.stdout)
    except ValueError:
        # Do not copy arbitrary child output/environment into the evidence.
        result = {"error": "invalid_cli_json"}
    h.receipts.append({"args": list(map(str, args)), "exit": process.returncode, "result": result})
    return result


def check(h, name, actual, expected=True):
    # Record the complete observable failure, including missing business data,
    # rather than abandoning the RED artifact after its first failed assertion.
    h.assertions.append(
        {
            "name": name,
            "expected": expected,
            "actual": actual,
            "status": "PASS" if actual == expected else "FAIL",
            "evidence": "proxy-scenario.json",
        }
    )


def capture_run(h, receipt):
    if "run_id" not in receipt:
        return {"receipt": receipt}
    return {
        "receipt": receipt,
        "inspect": command(h, "inspect", "run", receipt["run_id"]),
        "export": command(h, "export", "proxy_environment", "--run", receipt["run_id"]),
    }


def business(exported):
    return {record["record_key"]: record["data"] for record in exported.get("records", [])}


def scenario(h):
    from seal_v12_site import rows

    version = recipe(h)
    binding = record_source(
        h,
        "proxy_environment",
        version,
        allowed_path_prefixes=["/api"],
        methods=["GET"],
        budget={"requests": 8, "seconds": 10, "response_bytes": 100000},
        concurrency=1,
    )
    # Deliberately poison both cases; never inherit a user's real proxy URL or
    # credentials into this test. Empty NO_PROXY forbids bypassing the defect.
    for key in PROXY_KEYS:
        h.env[key] = DUMMY_PROXY
    h.env["NO_PROXY"] = h.env["no_proxy"] = ""
    h.env["SEAL_ALLOW_LOOPBACK"] = "1"
    first = capture_run(h, command(h, "run", "proxy_environment", "--binding", binding))
    h.capture("proxy-collect", first)
    expected = {row["id"]: row for row in rows()}
    exported = first.get("export", {})
    check(
        h,
        "collect_complete_with_broken_environment_proxy",
        first["receipt"].get("status"),
        "complete",
    )
    check(h, "collect_ten_business_records", len(exported.get("records", [])), 10)
    check(h, "collect_independent_business_oracle", business(exported), expected)
    check(
        h,
        "source_received_direct_GET",
        h.site.ledger,
        [{"method": "GET", "path": "/api", "status": 200}],
    )
    check(
        h,
        "collect_raw_evidence_recomputable",
        verify_export(h.root / "archive", exported)
        if "records" in exported
        else ["missing_export"],
        [],
    )
    # Stop the actual Source before Replay. Even a reachable proxy cannot make
    # a network-backed Replay pass the independent original business oracle.
    requests_before = list(h.site.ledger)
    h.site.close()
    replay = capture_run(h, command(h, "replay", first["receipt"]["run_id"]))
    h.capture("proxy-replay", replay)
    replay_export = replay.get("export", {})
    replay_inspect = replay.get("inspect", {})
    check(h, "replay_complete_with_origin_stopped", replay["receipt"].get("status"), "complete")
    check(h, "replay_ten_original_business_records", len(replay_export.get("records", [])), 10)
    check(h, "replay_independent_business_oracle", business(replay_export), expected)
    check(h, "replay_source_ledger_unchanged", h.site.ledger, requests_before)
    check(
        h,
        "replay_zero_network_observations",
        replay_inspect.get("observations", ["missing_inspect"]),
        [],
    )
    check(
        h, "replay_zero_http_attempts", replay_inspect.get("discovery", {}).get("http_attempts"), 0
    )
    check(
        h,
        "replay_zero_downloader_requests",
        replay["receipt"].get("report", {}).get("stats", {}).get("downloader/request_count", 0),
        0,
    )
    check(
        h,
        "replay_raw_evidence_recomputable",
        verify_export(h.root / "archive", replay_export)
        if "records" in replay_export
        else ["missing_export"],
        [],
    )
    h.capture(
        "proxy-scenario",
        {
            "contract": "Environment HTTP proxies are ignored for Source transport and offline Replay",
            "proxy": DUMMY_PROXY,
            "proxy_keys": list(PROXY_KEYS),
            "no_proxy": "",
            "source": h.site.url + "/api",
            "data_premise": "Owned isolated loopback JSON Source; ten deterministic public synthetic entities",
            "collect_status": first["receipt"].get("status"),
            "collect_errors": first["receipt"].get("report", {}).get("errors", []),
            "collect_record_count": len(exported.get("records", [])),
            "replay_status": replay["receipt"].get("status", replay["receipt"].get("error")),
            "source_stopped_before_replay": True,
            "credentials": "none; only the local dummy endpoint is saved",
        },
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.chdir(ROOT)
    output = args.output
    if output.exists():
        output = output / datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")
    h, error = Harness(output), None
    try:
        h.start()
        scenario(h)
        failures = sum(item["status"] == "FAIL" for item in h.assertions)
        if failures:
            error = (
                f"environment proxy changed Source behavior: {failures} failed contract assertions"
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        h.save("environment-proxy", error)
        manifest_path = h.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(
            command="uv run --frozen python experiments/v1.2-acceptance/proxy_behavior.py --output <new-directory>",
            scope="Real seal CLI + Scrapy + isolated PostgreSQL; deterministic loopback Source and refused local proxy",
            expected="complete collect with ten independently verified records and zero-network Replay",
            status="RED" if error else "GREEN",
            error=error,
        )
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        h.close()
    print(
        json.dumps(
            {"status": "RED" if error else "GREEN", "artifact": str(h.output), "error": error},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return int(error is not None)


if __name__ == "__main__":
    raise SystemExit(main())
