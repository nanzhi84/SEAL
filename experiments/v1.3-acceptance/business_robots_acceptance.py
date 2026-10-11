"""Before implementation: policy redirect/absence failures through CLI, PG and HTTP.

Unapproved HTML, changed hashes, actual Disallow, 403/5xx and an out-of-scope
redirect must block. Reviewed exact missing HTML and approved bounded redirects
may admit a public document. Every hop is archived and Replay makes zero GETs.
"""

import argparse
import hashlib
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/v1.2-acceptance"))
from acceptance import Harness  # noqa: E402


def exercise(h):
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    mode = ["allow"]
    missing = b"<html><title>Missing policy</title><p>Page not found</p></html>"

    def get(request):
        status, location, content_type = 200, None, "text/plain"
        body = b"User-agent: *\nAllow: /\n"
        if request.path == "/robots.txt":
            if mode[0] in ("missing", "changed", "challenge"):
                body = missing if mode[0] == "missing" else b"<html>Sign in</html>"
            elif mode[0] in ("redirect", "outside", "loop"):
                status = 302
                location = {"redirect": "/policy", "outside": "/unapproved", "loop": "/robots.txt"}[
                    mode[0]
                ]
                body = b""
            elif mode[0] == "deny":
                body = b"User-agent: *\nDisallow: /\n"
            elif mode[0] in ("403", "503", "404"):
                status, body = int(mode[0]), b""
        elif request.path == "/business/document":
            content_type = "text/html"
            body = b"<html><h1>Public contract</h1><article><p>Full public business document retained and checked from archived bytes.</p></article></html>"
        elif request.path != "/policy":
            return original(request)
        h.site.ledger.append({"method": "GET", "path": request.path, "status": status})
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        if location:
            request.send_header("Location", location)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = get
    try:
        version = h.cli("recipe", "pack", ROOT / "recipes/seeded")["recipe_version"]
        for scenario in (
            "allow",
            "missing",
            "changed",
            "challenge",
            "redirect",
            "outside",
            "loop",
            "deny",
            "403",
            "503",
            "404",
        ):
            mode[0] = scenario
            name = "business_policy_" + scenario
            h.config(
                name,
                entries=[h.site.url + "/business/document"],
                allowed_path_prefixes=["/business"],
                output_schema="record.v1",
                robots=True,
                delay=0.0,
                robots_redirect_paths=["/policy"],
                robots_absent_responses=[
                    {
                        "url": h.site.url + "/robots.txt",
                        "body_sha256": hashlib.sha256(missing).hexdigest(),
                    }
                ],
            )
            binding = h.binding(
                name,
                version,
                {
                    "expand_homepage": False,
                    "discover_sitemaps": False,
                    "specific_rules": [{"title": "h1", "body": "article", "date": None}],
                },
            )
            before = len(h.site.ledger)
            permitted = scenario in {"allow", "missing", "redirect", "404"}
            run = h.cli("collect", name, "--binding", binding, ok=permitted)
            inspected = h.cli("inspect", "run", run["run_id"])
            exported = h.cli("export", name, "--run", run["run_id"])
            h.check(
                scenario + "_business_request",
                any(x["path"] == "/business/document" for x in h.site.ledger[before:]),
                permitted,
            )
            h.check(scenario + "_record_count", len(exported["records"]), int(permitted))
            h.check(scenario + "_status", run["status"], "complete" if permitted else "partial")
            h.capture(scenario, {"run": run, "inspect": inspected, "export": exported})
            if scenario in {"missing", "redirect"}:
                before_replay = len(h.site.ledger)
                replay = h.cli("replay", run["run_id"])
                h.check(scenario + "_replay_status", replay["status"], "complete")
                h.check(scenario + "_replay_zero_http", len(h.site.ledger), before_replay)
                h.capture(scenario + "-replay", replay)
    finally:
        handler.do_GET = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    h, error = Harness(args.output), None
    try:
        h.start()
        exercise(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("business-robots", error)
        manifest_path = args.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "v1.3"
        manifest["command"] = (
            "uv run --frozen python experiments/v1.3-acceptance/business_robots_acceptance.py --output <new-directory>"
        )
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        h.close()
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
