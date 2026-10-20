"""Fresh browser-method contract E2E; no product implementation is inspected.

Failure boundaries fixed before the product fix:
M1. A rendered_dom public GET page and its external GET script must execute,
    retain complete rendered input and reach Snapshot/API with original and
    Markdown bytes equal to their CAS hashes (positive control).
M2. Sufficient static legal text plus an external script that synchronously
    POSTs x=1 to /action is not an authorized query. The synchronous XHR runs
    before load, so wait_until=load cannot omit its completion as a timing race.
    /action must receive zero POST requests, the
    CLI must explicitly fail, and no successful Version, delivery row, complete
    Run or published result may be produced for this source.
M3. Rejecting that action must retain an explicit diagnostic failure fact, not
    silently ignore it and deliver the page's sufficient static text.

This does not cover Adapter-declared replayable POST queries; those remain an
HTTP acquisition contract and have separate real-source acceptance evidence.

Run: .venv/bin/python tests/issue31_browser_methods.py --run baseline
Prerequisites: root .venv; Docker; postgres:17-alpine; local MinIO image.
Random tmpfs PostgreSQL/MinIO, loopback fixtures and a dedicated bucket only.
No .env.local or business environment is read. Generated data is disposable
under work/ and artifacts/; artifact credentials are redacted and scanned.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from datetime import datetime, timezone

from issue31_browser_budget import (
    STATIC, facts, no_partial, provenance,
    test_real_browser_inputs_and_published_bytes,
)
from issue31_common import collect
from issue31_support import ROOT, Api, Fixture, Lab, base_source, html_page, run_tests, sha256


class MethodFixture(Fixture):
    """Real HTTP fixture recording methods, including a successful POST target."""

    def __init__(self):
        super().__init__()
        self.calls = []
        fixture = self
        handler = self.server.RequestHandlerClass
        original_get = handler.do_GET

        def get(request):
            fixture.calls.append({"method": "GET", "path": request.path})
            return original_get(request)

        def post(request):
            length = int(request.headers.get("Content-Length") or 0)
            body = request.rfile.read(length) if length else b""
            fixture.calls.append({"method": "POST", "path": request.path,
                                  "body_bytes": len(body), "body_sha256": sha256(body)})
            fixture.requests.append(request.path)
            fixture.hits[request.path] = fixture.hits.get(request.path, 0) + 1
            response = b"accepted"
            request.send_response(200)
            request.send_header("Content-Type", "text/plain")
            request.send_header("Content-Length", str(len(response)))
            request.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                request.wfile.write(response)

        handler.do_GET = get
        handler.do_POST = post


def test_safe_get_renders_and_reaches_snapshot_api(lab):
    """Real external GET scripts must still render and deliver byte-identical files."""
    test_real_browser_inputs_and_published_bytes(lab)


def test_unannounced_script_post_never_reaches_source(lab):
    """A page-script POST must fail closed before any mutation reaches the source."""
    sid, fx = "dd-183", MethodFixture()
    marker = "EXTERNAL SAFE GET SCRIPT EXECUTED BEFORE ACTION"
    script = (
        "document.querySelector('#required').insertAdjacentHTML('beforeend',"
        + json.dumps("<p>" + marker + "</p>") + ");\n"
        "const action = new XMLHttpRequest();\n"
        "action.open('POST', '/action', false);\n"
        "action.send('x=1');\n"
    )
    fx.put("/document", html_page("Public Legal Instrument With Action",
           "<p>" + STATIC + "</p><section id='required'></section>"
           "<script src='/required.js'></script>", extra_head="<link rel='icon' href='data:,'>"))
    fx.put("/required.js", script, "application/javascript")
    cfg = base_source(sid, fx, ["/document"], concurrency=1,
                      budget={"requests": 20, "response_bytes": 100000},
                      fetch={"mode": "rendered_dom", "max_depth": 0,
                             "wait_until": "load", "page_timeout_ms": 15000})
    lab.save("fixtures/unannounced-post.json", {
        "source_id": sid, "wait_until": "load", "static_text": STATIC,
        "script": script, "post_body_sha256": sha256(b"x=1"),
        "routes": {path: {"status": row[0], "bytes": len(row[2]), "sha256": sha256(row[2])}
                   for path, row in fx.routes.items()},
    })
    try:
        result, receipt, _ = collect(lab, sid, cfg, timeout=90, label="unannounced-browser-post")
        data = facts(lab, sid, fx, result, receipt)
        data["method_calls"] = list(fx.calls)
        data["cli_stdout"] = result.stdout
        data["cli_stderr"] = result.stderr
        lab.save("evidence/unannounced-post.json", data)
        with lab.soft():
            lab.check("public GET page really reached controlled source",
                      any(c == {"method": "GET", "path": "/document"} for c in fx.calls), fx.calls)
            lab.check("external action script really reached controlled source",
                      any(c == {"method": "GET", "path": "/required.js"} for c in fx.calls), fx.calls)
            posts = [c for c in fx.calls if c["method"] == "POST"]
            lab.check("unannounced POST never reaches source", not posts, posts)
            diagnostic = (result.stdout + "\n" + result.stderr).lower()
            lab.check("CLI explicitly fails the unsafe browser action", result.returncode != 0,
                      {"exit": result.returncode, "receipt": receipt,
                       "stderr": result.stderr[-1500:], "stdout": result.stdout[-1500:]})
            lab.check("CLI failure diagnostic identifies the unsafe method",
                      "post" in diagnostic and any(word in diagnostic for word in
                      ("unsafe", "unsupported", "not allowed", "forbidden", "blocked", "replay", "method")),
                      {"stdout": result.stdout[-1500:], "stderr": result.stderr[-1500:]})
            no_partial(lab, data, "unannounced browser POST")
            result, sealed = lab.cli_json("snapshot", "create", label="snapshot after rejected action")
            lab.check("Snapshot can still seal the safe positive control", result.returncode == 0 and
                      (sealed or {}).get("sealed") is True, sealed or result.stderr[-800:])
            snapshot_id = (sealed or {}).get("snapshot_id") or (sealed or {}).get("id")
            if snapshot_id:
                published, receipt = lab.cli_json("snapshot", "publish", snapshot_id,
                                                  label="publish after rejected browser action")
                lab.check("Snapshot publication succeeds", published.returncode == 0, receipt)
                api = Api(lab, {"browser-methods": [sid, "dd-247"]}).start()
                try:
                    items, _, snapshot = api.all_results("browser-methods", source_id=sid)
                    lab.save("evidence/rejected-source-api.json", {"snapshot_id": snapshot, "items": items})
                    lab.check("published API contains no rejected source result", not items, items)
                finally:
                    api.stop()
    finally:
        fx.close()


TESTS = [test_safe_get_renders_and_reaches_snapshot_api, test_unannounced_script_post_never_reaches_source]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()
    if not args.run.replace("-", "").replace("_", "").isalnum():
        parser.error("--run must contain only letters, digits, '-' and '_'")
    lab = Lab("issue31-browser-methods")
    lab.artifacts = ROOT / "artifacts" / "issue31-browser-methods" / args.run
    lab.artifacts.mkdir(parents=True, exist_ok=True)
    start = provenance()
    lab.save("failure-boundaries.txt", __doc__)
    lab.save("environment.json", {**start,
             "reproduce": ".venv/bin/python tests/issue31_browser_methods.py --run " + args.run,
             "prerequisites": "root .venv; Docker; postgres:17-alpine; seal-minio:RELEASE.2025-10-15T17-29-55Z",
             "isolation": "random-port tmpfs PostgreSQL/MinIO; random credentials; dedicated bucket; loopback fixtures; no .env.local"})
    print("artifacts:", lab.artifacts, flush=True)
    try:
        lab.start()
        result = lab.cli("init", label="init isolated database")
        if result.returncode:
            raise RuntimeError("isolated init failed: " + lab.redact(result.stderr[-1500:]))
        run_tests(lab, TESTS)
    finally:
        lab.stop()
        end = provenance()
        summary = {s: sum(r["status"] == s for r in lab.results) for s in ("PASS", "FAIL", "NOT VERIFIED")}
        lab.save("provenance-end.json", end)
        lab.save("summary.json", {"summary": summary, "results": lab.results,
                                  "source_changed_during_run": start["source_hashes"] != end["source_hashes"],
                                  "tests_changed_during_run": start["test_hashes"] != end["test_hashes"]})
        leaked = []
        for path in lab.artifacts.rglob("*"):
            if path.is_file() and any(value.encode() in path.read_bytes() for value in lab.secrets if value):
                leaked.append(str(path.relative_to(lab.artifacts)))
        lab.save("secret-scan.json", {"pass": not leaked, "secret_values_checked": len(lab.secrets),
                                     "leaking_files": leaked})
        hashes = {str(path.relative_to(lab.artifacts)): sha256(path.read_bytes())
                  for path in sorted(lab.artifacts.rglob("*")) if path.is_file()}
        lab.save("artifact-sha256.json", hashes)
        print(json.dumps({**summary, "secret_scan_pass": not leaked}, ensure_ascii=False), flush=True)
    return int(bool(summary["FAIL"] or summary["NOT VERIFIED"] or leaked))


if __name__ == "__main__":
    raise SystemExit(main())
