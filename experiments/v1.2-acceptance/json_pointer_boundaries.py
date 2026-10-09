"""Real CLI boundaries for oversized trees and standalone uncached JSON inputs.

The fixture freezes one 10-field business Record in a >64 MiB decoded tree, then
two different HTTP inputs whose public Pointer caller supplies no body hash.
Observable contracts are exact typed business data, immutable raw hashes, no
discarded Records, finite memory and released cache; parse counts are diagnostic.
"""

import argparse
import hashlib
import json
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from json_pointer_performance import PerformanceHarness
from runtime_support import by_key, pointer, recipe, record_source, run

NO_HASH_RECIPE = """import scrapy
from seal.helpers import json_records
from seal.record_validation import JsonInputCache, json_pointer


class NoHashSpider(scrapy.Spider):
    name = "no_hash_public_pointer"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.context = context
        self.cache = JsonInputCache()

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(seed["url"], callback=self.parse,
                                 meta={"seal_role": seed["role"]})

    def parse(self, response):
        rows = json_pointer(response.body, "/data/data/dataList", cache=self.cache)
        for index, item in enumerate(json_records(response)):
            item["data"] = rows[index]
            yield item
"""


def row(number):
    return {
        "id": f"boundary-{number}",
        "name": f"Published boundary entity {number}",
        "active": number % 2 == 0,
        "count": number,
        "score": number + 0.5,
        "optional": None,
        "tags": ["public", str(number)],
        "address": {"city": "Singapore", "rank": number},
        "a/b": f"Slash key {number}",
        "til~de": f"Tilde key {number}",
    }


class BoundarySite:
    def __init__(self):
        self.ledger = []
        self.bodies = {
            "/api/oversized": json.dumps(
                {
                    "data": {"data": {"dataList": [row(0)]}},
                    "unselected_public_integer_envelope": list(range(2000000)),
                }
            ).encode(),
            "/api/nohash-first": json.dumps({"data": {"data": {"dataList": [row(1)]}}}).encode(),
            "/api/nohash-second": json.dumps({"data": {"data": {"dataList": [row(2)]}}}).encode(),
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                body = owner.bodies.get(self.path, b"not found")
                status = 200 if self.path in owner.bodies else 404
                owner.ledger.append(
                    {
                        "path": self.path,
                        "method": "GET",
                        "status": status,
                        "bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
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


class BoundaryHarness(PerformanceHarness):
    def __init__(self, output):
        super().__init__(output)
        self.site.close()
        self.site = BoundarySite()

    def save(self, stage, error=None):
        super().save(stage, error)
        path = self.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["scope"] = (
            "Oversized input and two no-hash public Pointer inputs through real CLI/Scrapy/isolated PG"
        )
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


def frozen(h, name, receipt, inspected, exported, expected_rows):
    h.check(name + "_complete", receipt["status"], "complete")
    h.check(name + "_no_diagnostics", receipt["errors"], [])
    records = by_key(exported)
    expected = {value["id"]: value for value in expected_rows}
    h.check(name + "_exact_keys", sorted(records), sorted(expected))
    h.check(
        name + "_typed_business_data",
        json.dumps({key: value["data"] for key, value in records.items()}, sort_keys=True),
        json.dumps(expected, sort_keys=True),
    )
    snapshots = {}
    for entry in inspected["run"]["inputs"]:
        key = entry["snapshot_id"]
        path = h.root / "archive/objects" / key[:2] / key[2:]
        raw_snapshot = path.read_bytes()
        h.check(name + "_snapshot_hash_" + key, hashlib.sha256(raw_snapshot).hexdigest(), key)
        snapshot = json.loads(raw_snapshot)
        body_key = snapshot["body_hash"]
        raw = (h.root / "archive/objects" / body_key[:2] / body_key[2:]).read_bytes()
        h.check(name + "_body_hash_" + key, hashlib.sha256(raw).hexdigest(), body_key)
        path = snapshot["url"].removeprefix(h.site.url)
        h.check(name + "_real_raw_bytes_" + key, raw == h.site.bodies[path])
        snapshots[key] = json.loads(raw)
    for result in inspected["record_results"]:
        candidate = result["candidate"]
        raw = snapshots[candidate["primary_snapshot_id"]]
        h.check(name + "_ten_fields_" + candidate["record_key"], len(candidate["data"]), 10)
        for field, value in candidate["data"].items():
            located = pointer(raw, candidate["locators"][field]["pointer"])
            h.check(
                name + "_typed_field_" + candidate["record_key"] + "_" + field,
                type(value) is type(located)
                and json.dumps(value, sort_keys=True) == json.dumps(located, sort_keys=True),
            )
        h.check(
            name + "_key_" + candidate["record_key"],
            str(pointer(raw, candidate["key_locator"]["pointer"])),
            candidate["record_key"],
        )
    stats = inspected["run"]["report"]["stats"]
    h.check(name + "_released_entries", stats.get("seal/record_json_retained_entries"), 0)
    h.check(name + "_released_bytes", stats.get("seal/record_json_retained_bytes"), 0)
    h.check(
        name + "_bounded_retained_memory",
        stats.get("seal/record_json_peak_bytes", 0) <= 64 * 1024 * 1024,
    )
    h.capture(name, {"receipt": receipt, "inspect": inspected, "export": exported})


def exercise(h):
    version = recipe(h)
    binding = record_source(
        h,
        "oversized-json",
        version,
        paths=["/api/oversized"],
        allowed_path_prefixes=["/api/oversized"],
        concurrency=1,
        budget={"requests": 1, "seconds": 120, "response_bytes": 30000000},
    )
    receipt, inspected, exported = run(h, "oversized-json", binding)
    frozen(h, "oversized", receipt, inspected, exported, [row(0)])
    h.check("oversized_finite_process_memory", h.timings[-1]["peak_rss_bytes"] < 768 * 1024 * 1024)
    h.check("oversized_one_network_request", len(h.site.ledger), 1)
    before = len(h.site.ledger)
    replay = h.cli("replay", receipt["run_id"])
    replay_inspected = h.cli("inspect", "run", replay["run_id"])
    replay_exported = h.cli("export", "oversized-json", "--run", replay["run_id"])
    frozen(h, "oversized-replay", replay, replay_inspected, replay_exported, [row(0)])
    h.check("oversized_replay_offline", len(h.site.ledger), before)
    root = h.root / "nohash-recipe"
    root.mkdir()
    (root / "recipe.py").write_text(NO_HASH_RECIPE)
    (root / "recipe.yaml").write_text(
        json.dumps(
            {
                "family": "no-hash-evidence",
                "entrypoint": "recipe:NoHashSpider",
                "params_schema": {"type": "object"},
            }
        )
    )
    version = h.cli("recipe", "pack", root)["recipe_version"]
    binding = record_source(
        h,
        "nohash-json",
        version,
        paths=["/api/nohash-first", "/api/nohash-second"],
        allowed_path_prefixes=["/api/nohash-first", "/api/nohash-second"],
        concurrency=1,
        budget={"requests": 2, "seconds": 30, "response_bytes": 30000},
    )
    receipt, inspected, exported = run(h, "nohash-json", binding)
    frozen(h, "nohash-two-inputs", receipt, inspected, exported, [row(1), row(2)])
    h.check("nohash_exact_two_requests", len(h.site.ledger) - before, 2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    h = BoundaryHarness(args.output)
    error = None
    try:
        h.start()
        exercise(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.capture(
            "summary",
            {
                "status": "FAIL" if error else "PASS",
                "timings": h.timings,
                "assertions": {
                    "total": len(h.assertions),
                    "passed": sum(item["status"] == "PASS" for item in h.assertions),
                },
                "fixtures": {
                    path: {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                    for path, body in h.site.bodies.items()
                },
                "diagnostic_scope": "strict native validation parse count excludes Recipe/helper parsing; oversized input not retained in Crawl LRU",
            },
        )
        h.save("json-pointer-boundaries", error)
        h.close()
    print(args.output.resolve())
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
