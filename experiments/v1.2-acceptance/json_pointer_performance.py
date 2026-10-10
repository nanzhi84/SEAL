"""Real CLI benchmark with independently recomputed typed JSON business evidence.

Failures frozen before implementation: repeated parsing exceeds a measured CPU
budget; one shared response loses rows/fields/types/keys; changed bytes reuse an
old tree; Replay changes values or accesses the network. Existing joint scenarios
retain invalid locator, corrupt archive, ambiguous key and type rejection duties.
"""

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from acceptance import Harness
from runtime_support import by_key, pointer, recipe, record_source, run


def oracle(revision=0):
    return [
        {
            "id": f"public-{i:04}",
            "name": f"Published business entity {i}",
            "active": i % 2 == 0,
            "count": i + revision,
            "score": i + 0.5,
            "optional": None,
            "tags": ["public", str(i)],
            "address": {"city": "Singapore", "rank": i},
            "a/b": f"Slash key {i}",
            "til~de": f"Tilde key {i}",
        }
        for i in range(1000)
    ]


def payload(revision):
    return json.dumps({"data": {"data": {"dataList": oracle(revision)}}}).encode()


class PerformanceSite:
    def __init__(self):
        self.ledger, self.revision = [], 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = payload(owner.revision)
                status = 200 if self.path == "/api/performance" else 404
                owner.ledger.append(
                    {
                        "method": "GET",
                        "path": self.path,
                        "status": status,
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "bytes": len(body),
                    }
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PerformanceHarness(Harness):
    def __init__(self, output):
        super().__init__(output)
        self.site.close()
        self.site = PerformanceSite()
        self.timings = []
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            self.env.pop(name, None)

    def cli(self, *args, ok=True):
        darwin = platform.system() == "Darwin"
        measure = args[0] in {"run", "replay"}
        command = [sys.executable, "-m", "seal", *map(str, args)]
        if measure:
            command = ["/usr/bin/time", "-l" if darwin else "-v", *command]
        started = time.monotonic()
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=240)
        elapsed = time.monotonic() - started
        try:
            data = json.loads(result.stdout)
        except ValueError:
            data = {"invalid_stdout": result.stdout[-1000:], "stderr": result.stderr[-2000:]}
        self.receipts.append(
            {"args": list(map(str, args)), "exit": result.returncode, "result": data}
        )
        if measure:
            if darwin:
                match = re.search(r"([0-9.]+) real\s+([0-9.]+) user\s+([0-9.]+) sys", result.stderr)
                rss = re.search(r"(\d+)\s+maximum resident set size", result.stderr)
                cpu = float(match[2]) + float(match[3]) if match else None
                peak = int(rss[1]) if rss else None
            else:
                user = re.search(r"User time \(seconds\): ([0-9.]+)", result.stderr)
                system = re.search(r"System time \(seconds\): ([0-9.]+)", result.stderr)
                rss = re.search(r"Maximum resident set size \(kbytes\): (\d+)", result.stderr)
                cpu = float(user[1]) + float(system[1]) if user and system else None
                peak = int(rss[1]) * 1024 if rss else None
            timing = {
                "args": list(map(str, args)),
                "run_id": data.get("run_id"),
                "wall_seconds": round(elapsed, 6),
                "cpu_seconds": cpu,
                "peak_rss_bytes": peak,
                "exit": result.returncode,
            }
            self.timings.append(timing)
            self.capture("timings-progress", self.timings)
            (self.output / f"timing-{len(self.timings)}.stderr.txt").write_text(result.stderr)
            print(json.dumps(timing), flush=True)
        if bool(result.returncode) == ok:
            raise AssertionError(f"CLI exit contract: {args}; {data}")
        return data

    def save(self, stage, error=None):
        super().save(stage, error)
        path = self.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(
            command=[sys.executable, *sys.argv],
            scope="1000 public loopback API Records × 10 typed fields, changed bytes and offline Replay",
            performance_summary="summary.json",
        )
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


def verify(h, name, receipt, inspected, exported, revision):
    expected = {row["id"]: row for row in oracle(revision)}
    records = by_key(exported)
    h.check(name + "_complete", receipt["status"], "complete")
    h.check(name + "_exact_keys", sorted(records), sorted(expected))
    h.check(
        name + "_exact_business_data", {key: row["data"] for key, row in records.items()}, expected
    )
    h.check(name + "_emissions", len(inspected["record_emissions"]), 1000)
    h.check(name + "_unique_input", len(inspected["run"]["inputs"]), 1)
    inputs = inspected["run"]["inputs"]
    snapshot_key = inputs[0]["snapshot_id"]
    objects = h.root / "archive/objects"

    def read(key):
        return (objects / key[:2] / key[2:]).read_bytes()

    snapshot_bytes = read(snapshot_key)
    h.check(name + "_snapshot_hash", hashlib.sha256(snapshot_bytes).hexdigest(), snapshot_key)
    snapshot = json.loads(snapshot_bytes)
    raw = read(snapshot["body_hash"])
    h.check(name + "_original_bytes", raw, payload(revision))
    h.check(name + "_body_hash", hashlib.sha256(raw).hexdigest(), snapshot["body_hash"])
    document = json.loads(raw)
    field_count = 0
    for result in inspected["record_results"]:
        candidate = result["candidate"]
        key = candidate["record_key"]
        h.check(name + "_ten_fields_" + key, len(candidate["data"]), 10)
        h.check(
            name + "_business_key_" + key,
            str(pointer(document, candidate["key_locator"]["pointer"])),
            key,
        )
        for field, actual in candidate["data"].items():
            located = pointer(document, candidate["locators"][field]["pointer"])
            h.check(
                name + "_typed_field_" + key + "_" + field,
                type(located) is type(actual)
                and json.dumps(located, sort_keys=True, allow_nan=False)
                == json.dumps(actual, sort_keys=True, allow_nan=False)
                and type(actual) is type(expected[key][field])
                and json.dumps(actual, sort_keys=True, allow_nan=False)
                == json.dumps(expected[key][field], sort_keys=True, allow_nan=False),
            )
            field_count += 1
    h.check(name + "_all_field_projections", field_count, 10000)
    h.capture(name, {"receipt": receipt, "inspect": inspected, "export": exported})
    return records


def exercise(h, include_changed=True):
    version = recipe(h)
    binding = record_source(
        h,
        "json-performance",
        version,
        paths=["/api/performance"],
        allowed_path_prefixes=["/api/performance"],
        concurrency=4,
        budget={"requests": 2, "seconds": 180, "response_bytes": 2000000},
    )
    first, inspected, exported = run(h, "json-performance", binding)
    initial = verify(h, "collect-first", first, inspected, exported, 0)
    h.check("first_one_http_request", len(h.site.ledger), 1)
    if include_changed:
        h.site.revision = 1
        second, inspected, exported = run(h, "json-performance", binding)
        updated = verify(h, "collect-changed", second, inspected, exported, 1)
        h.check(
            "same_url_changed_body_new_values",
            all(
                updated[key]["data"]["count"] == initial[key]["data"]["count"] + 1
                for key in initial
            ),
        )
        h.check(
            "same_url_changed_body_stable_ids",
            {key: row["record_id"] for key, row in updated.items()},
            {key: row["record_id"] for key, row in initial.items()},
        )
        h.check("changed_exactly_two_http_requests", len(h.site.ledger), 2)
    network_before = len(h.site.ledger)
    replay = h.cli("replay", first["run_id"])
    inspected = h.cli("inspect", "run", replay["run_id"])
    exported = h.cli("export", "json-performance", "--run", replay["run_id"])
    verify(h, "replay-original", replay, inspected, exported, 0)
    h.check("replay_zero_http", len(h.site.ledger), network_before)
    h.check("replay_zero_observations", inspected["observations"], [])
    return version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--baseline-only", action="store_true")
    args = parser.parse_args()
    h = PerformanceHarness(args.output)
    error, version = None, None
    try:
        h.start()
        version = exercise(h, include_changed=not args.baseline_only)
        if args.baseline:
            baseline = json.loads(args.baseline.read_text())
            for current in h.timings:
                prior = next(
                    row for row in baseline["timings"] if row["args"][0] == current["args"][0]
                )
                if current["args"][0] == "run" and current != h.timings[0]:
                    continue
                h.check(
                    "cpu_reduction_" + current["args"][0],
                    current["cpu_seconds"] <= prior["cpu_seconds"] * 0.65,
                )
                h.check(
                    "bounded_rss_" + current["args"][0],
                    current["peak_rss_bytes"] <= prior["peak_rss_bytes"] * 1.5,
                )
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        summary = {
            "artifact_version": "1",
            "command": "uv run --frozen python "
            "experiments/v1.2-acceptance/json_pointer_performance.py --output <new-directory>"
            " [--baseline <baseline-summary.json>] [--baseline-only]",
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "database": "isolated loopback PostgreSQL",
                "concurrency": 4,
            },
            "dataset": {
                "records": 1000,
                "fields_per_record": 10,
                "original_bytes": len(payload(0)),
                "original_sha256": hashlib.sha256(payload(0)).hexdigest(),
            },
            "performance_contract": {
                "cpu_ratio_max": 0.65,
                "rss_ratio_max": 1.5,
                "scope": "first collect and original Replay CLI process tree",
            },
            "phase": "baseline" if args.baseline_only else "optimized",
            "recipe_version": version,
            "timings": h.timings,
            "assertions": {
                "total": len(h.assertions),
                "passed": sum(a["status"] == "PASS" for a in h.assertions),
            },
            "status": "FAIL" if error else "PASS",
        }
        h.capture("summary", summary)
        h.save("json-pointer-performance", error)
        h.close()
    print(args.output.resolve(), flush=True)
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
