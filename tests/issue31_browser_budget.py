"""Fresh #31 browser/budget E2E; no product module or implementation is read.

Failure boundaries fixed before implementation:
B1. Static legal text hides a blocked necessary external script: any Version,
    delivery row, complete run, or real request beyond the one-request cap fails.
B2. Sufficient budget must run both external scripts, including a redirect, and
    archive every real HTTP response (robots/main/scripts/hops) independently.
    The separate rendered_dom input must retain complete UTF-8 DOM, and sealed
    Snapshot/API files must equal CAS bytes and their recorded SHA-256.
B3. A necessary script returning HTTP 500 or exceeding the byte cap cannot
    deliver static-only legal text as a successful Version or complete run.
B4. SIGKILL followed by a real worker takeover must preserve consumed requests,
    including the in-flight request; a new attempt must not replenish the cap.

Run: .venv/bin/python tests/issue31_browser_budget.py --run baseline
Prerequisites: root .venv, Docker, postgres:17-alpine and the local MinIO image.
Only random tmpfs PostgreSQL/MinIO, loopback sources and a dedicated bucket are
used; no .env.local or business database is read. The worker lease expires
naturally after 32 seconds. Generated data stays under work/ and artifacts/.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from issue31_common import collect, current_rows
from issue31_support import ROOT, Api, Fixture, Lab, base_source, html_page, run_tests, sha256

MARKERS = ["EXTERNAL SCRIPT EXECUTED ARTICLE SEVEN", "REQUIRED REDIRECTED SCRIPT ARTICLE EIGHT"]
STATIC = " ".join(
    f"Article {n}. A regulated entity shall retain complete public records and provide an accurate report to the authority."
    for n in range(1, 16)
)


def browser_source(lab, sid, *, requests=20, response_bytes=100000, robots=False, fail=None):
    fx = Fixture()
    fx.robots()
    page = html_page("Public Legal Instrument", "<p>" + STATIC + "</p>"
                     "<section id='required'></section><script src='/first.js'></script>"
                     "<script src='/redirect.js'></script>", extra_head="<link rel='icon' href='data:,'>")
    fx.put("/document", page)
    fx.put("/first.js", f"document.querySelector('#required').insertAdjacentHTML('beforeend','<p>{MARKERS[0]}</p>');",
           "application/javascript")
    fx.put("/redirect.js", b"", "application/javascript", status=302, headers={"Location": "/second.js"})
    script = f"document.querySelector('#required').insertAdjacentHTML('beforeend','<p>{MARKERS[1]}</p>');"
    if fail == "oversize":
        script = "/*" + "x" * 12000 + "*/\n" + script
    fx.put("/second.js", script if fail != "500" else "required legal script temporarily unavailable",
           "application/javascript", status=500 if fail == "500" else 200)
    cfg = base_source(sid, fx, ["/document"], robots=robots, concurrency=1,
                      budget={"requests": requests, "response_bytes": response_bytes},
                      fetch={"mode": "rendered_dom", "max_depth": 0, "page_timeout_ms": 15000})
    lab.save(f"fixtures/{sid}.json", {p: {"status": r[0], "bytes": len(r[2]), "sha256": sha256(r[2])}
                                   for p, r in fx.routes.items()})
    return fx, cfg


def facts(lab, sid, fx, result=None, receipt=None):
    data = {
        "source_id": sid, "requests": list(fx.requests), "request_counts": dict(Counter(fx.requests)),
        "acquisitions": lab.q("SELECT * FROM seal.acquisition WHERE source_id=%s ORDER BY started_at,id", (sid,)),
        "runs": lab.q("SELECT id,status,attempt_epoch,errors,report FROM seal.run WHERE source_id=%s ORDER BY created_at", (sid,)),
        "versions": lab.q("SELECT v.id,v.inputs_ref FROM seal.version v JOIN seal.resource r ON r.id=v.resource_id "
                          "JOIN seal.frontier f ON f.id=r.frontier_id WHERE f.source_id=%s", (sid,)),
        "rows": current_rows(lab, sid), "receipt": receipt,
    }
    if result:
        data["cli_exit"] = result.returncode
    lab.save(f"evidence/{sid}.json", data)
    return data


def no_partial(lab, data, reason):
    lab.check(reason + ": no successful Version", not data["versions"], data["versions"])
    lab.check(reason + ": no delivery row", not data["rows"], [{"file_hash": r["file_hash"]} for r in data["rows"]])
    lab.check(reason + ": no complete run", data["runs"] and all(r["status"] != "complete" for r in data["runs"]), data["runs"])
    failed = [a for a in data["acquisitions"] if a["state"] in {"failed", "cancelled", "abandoned"} and (a["error"] or a["outcome"])]
    errors = any(r["errors"] for r in data["runs"])
    lab.check(reason + ": explicit failure fact", bool(failed) or errors, data["acquisitions"])


def test_one_request_cannot_deliver_static_body(lab):
    """One request cannot load necessary JS or silently deliver static-only law."""
    sid = "dd-058"
    fx, cfg = browser_source(lab, sid, requests=1)
    try:
        result, receipt, _ = collect(lab, sid, cfg, timeout=90, label="browser-one-request")
        data = facts(lab, sid, fx, result, receipt)
        with lab.soft():
            lab.check("one-request fixture really received main HTML", fx.hits.get("/document") == 1, fx.requests)
            lab.check("all browser network traffic respects requests=1", len(fx.requests) <= 1, fx.requests)
            no_partial(lab, data, "necessary script blocked by request cap")
    finally:
        fx.close()


def verify_http_originals(lab, data, fx):
    actual = Counter(fx.requests)
    http = [a for a in data["acquisitions"] if a["acquisition_kind"] != "rendered_dom"]
    recorded = Counter(a["request_url"].removeprefix(fx.base) for a in http)
    lab.check("each real HTTP request has exactly one separate Acquisition", actual == recorded,
              {"real": dict(actual), "facts": dict(recorded)})
    for path in actual:
        route = fx.routes.get(path, (404, "text/plain", b"not found"))
        digest = sha256(route[2])
        rows = [a for a in http if a["request_url"] == fx.base + path]
        lab.check(path + ": response status and original hash retained",
                  rows and all(a["http_status"] == route[0] and a["body_hash"] == digest and a["archive_ref"] for a in rows),
                  [{k: a[k] for k in ("http_status", "state", "body_hash", "archive_ref")} for a in rows])
        key = lab.cas_key_for(digest)
        lab.check(path + ": original response CAS byte-identical", key is not None and lab.s3_get(key) == route[2], digest)
    redirect = next((a for a in http if a["request_url"] == fx.base + "/redirect.js"), None)
    target = next((a for a in http if a["request_url"] == fx.base + "/second.js"), None)
    lab.check("script redirect hop has explicit lineage", redirect and target and
              target["parent_acquisition_id"] == redirect["id"] and target["parent_relation"] == "redirect",
              [{k: a[k] for k in ("id", "request_url", "parent_acquisition_id", "parent_relation")} for a in http])


def snapshot_api(lab, sid, row):
    result, receipt = lab.cli_json("snapshot", "create", label="browser snapshot create")
    lab.check("browser success can seal Snapshot", result.returncode == 0 and (receipt or {}).get("sealed") is True,
              receipt or result.stderr[-800:])
    snapshot_id = (receipt or {}).get("snapshot_id") or (receipt or {}).get("id")
    if not snapshot_id:
        return
    result, receipt = lab.cli_json("snapshot", "publish", snapshot_id, label="browser snapshot publish")
    lab.check("browser Snapshot publication succeeds", result.returncode == 0, receipt or result.stderr[-800:])
    api = Api(lab, {"browser": [sid]}).start()
    try:
        items, _, published = api.all_results("browser", source_id=sid)
        lab.save("evidence/api-results.json", {"snapshot_id": published, "items": items})
        lab.check("published API includes exactly the successful browser row", len(items) == 1 and
                  items[0]["文件哈希"] == row["file_hash"] and str(published) == str(snapshot_id), items)
        for name, digest in (("original", row["file_hash"]), ("markdown", row["markdown_hash"])):
            response = api.get("/files/" + digest, "browser")
            cas = lab.s3_get(digest)
            lab.check(name + ": API bytes equal CAS and recorded SHA-256", response.status_code == 200 and
                      response.content == cas and sha256(response.content) == digest,
                      {"status": response.status_code, "sha256": sha256(response.content), "recorded": digest})
    finally:
        api.stop()


def test_real_browser_inputs_and_published_bytes(lab):
    """Robots/main/two JS/redirect are real inputs; rendered DOM reaches API."""
    sid = "dd-247"
    fx, cfg = browser_source(lab, sid, robots=True)
    try:
        result, receipt, _ = collect(lab, sid, cfg, timeout=90, label="browser-success")
        data = facts(lab, sid, fx, result, receipt)
        with lab.soft():
            lab.check("browser collect succeeds", result.returncode == 0, receipt or result.stderr[-800:])
            lab.check("robots/main/two scripts/redirect really fetched", all(fx.hits.get(p) == 1 for p in
                      ("/robots.txt", "/document", "/first.js", "/redirect.js", "/second.js")), fx.requests)
            verify_http_originals(lab, data, fx)
            lab.check("exactly one successful browser delivery", len(data["rows"]) == 1, data["rows"])
            if len(data["rows"]) == 1:
                row = data["rows"][0]
                raw, markdown = lab.s3_get(row["file_hash"]), lab.s3_get(row["markdown_hash"])
                dom = raw.decode("utf-8")
                lab.check("both external scripts changed delivered Markdown", all(m in markdown.decode("utf-8") for m in MARKERS),
                          markdown.decode("utf-8")[-2000:])
                lab.check("rendered DOM is complete UTF-8 HTML with both necessary clauses", dom.rstrip().endswith("</html>") and
                          STATIC in dom and all(m in dom for m in MARKERS) and "\ufffd" not in dom, dom[-1500:])
                derived = [a for a in data["acquisitions"] if a["acquisition_kind"] == "rendered_dom"]
                lab.check("rendered DOM is a separate derived input", derived and any(a["body_hash"] == row["file_hash"] for a in derived)
                          and row["file_hash"] != sha256(fx.routes["/document"][2]), derived)
                lab.check("rendered DOM original CAS hashes correctly", sha256(raw) == row["file_hash"], row["file_hash"])
                snapshot_api(lab, sid, row)
    finally:
        fx.close()


def test_necessary_script_500_blocks_version(lab):
    """A necessary external script's HTTP 500 cannot become static-only success."""
    sid = "dd-183"
    fx, cfg = browser_source(lab, sid, fail="500")
    try:
        result, receipt, _ = collect(lab, sid, cfg, timeout=90, label="browser-script-500")
        data = facts(lab, sid, fx, result, receipt)
        with lab.soft():
            lab.check("necessary script's HTTP 500 really reached source", fx.hits.get("/second.js") == 1, fx.requests)
            no_partial(lab, data, "necessary script HTTP 500")
    finally:
        fx.close()


def test_necessary_script_byte_cap_blocks_version(lab):
    """Oversized external script fails with no partial original or static Version."""
    sid = "dd-155"
    fx, cfg = browser_source(lab, sid, response_bytes=4096, fail="oversize")
    try:
        result, receipt, _ = collect(lab, sid, cfg, timeout=90, label="browser-script-byte-cap")
        data = facts(lab, sid, fx, result, receipt)
        with lab.soft():
            lab.check("oversized script really requested", fx.hits.get("/second.js") == 1, fx.requests)
            no_partial(lab, data, "necessary script byte cap")
            script = [a for a in data["acquisitions"] if a["request_url"] == fx.base + "/second.js"]
            lab.check("oversized script has explicit failed Acquisition without partial original claim", script and
                      all(a["state"] == "failed" and a["error"] and a["archive_ref"] is None for a in script), script)
    finally:
        fx.close()


def test_worker_takeover_preserves_consumed_requests(lab):
    """A real worker takeover cannot repeat the cap's already-started request."""
    sid, fx, gate = "dd-066", Fixture(), threading.Event()
    fx.put("/first.txt", "First public legal record. " * 20, "text/plain")
    fx.put("/pending.txt", "Pending public legal record. " * 20, "text/plain")
    fx.hang["/pending.txt"] = gate
    cfg = base_source(sid, fx, ["/first.txt", "/pending.txt"], concurrency=1,
                      budget={"requests": 2, "seconds": 300}, fetch={"max_depth": 0})
    child = None
    try:
        path = lab.source_file(sid, cfg)
        result, receipt = lab.cli_json("collect", path, "--enqueue", "--no-progress", label="enqueue budget takeover")
        lab.check("real job enqueued", result.returncode == 0, receipt or result.stderr[-800:])
        child = lab.popen("worker", "--once")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not fx.hits.get("/pending.txt") and child.poll() is None:
            time.sleep(.1)
        lab.check("old worker reached the second real request", fx.hits.get("/pending.txt") == 1 and len(fx.requests) == 2,
                  {"requests": fx.requests, "log": Path(child.logfile).read_text()[-800:]})
        child.send_signal(signal.SIGKILL)
        child.wait(10)
        before = facts(lab, sid, fx)
        lab.save("evidence/takeover-before.json", before)
        fx.hang.pop("/pending.txt", None)
        time.sleep(32)
        result, receipt = lab.cli_json("worker", "--once", timeout=60, label="takeover budget worker")
        gate.set()
        after = facts(lab, sid, fx, result, receipt)
        lab.save("evidence/takeover-after.json", after)
        with lab.soft():
            lab.check("takeover uses same run with higher fenced epoch", len(before["runs"]) == len(after["runs"]) == 1 and
                      before["runs"][0]["id"] == after["runs"][0]["id"] and
                      after["runs"][0]["attempt_epoch"] > before["runs"][0]["attempt_epoch"], after["runs"])
            lab.check("takeover keeps exact remaining request budget (zero)", len(fx.requests) == 2, fx.requests)
            pending = [r for r in after["rows"] if r["public_url"] == fx.base + "/pending.txt"]
            lab.check("budget-exhausted pending document has no delivery", not pending, pending)
            lab.check("budget-exhausted takeover is not complete", all(r["status"] != "complete" for r in after["runs"]), after["runs"])
            old = [a["id"] for a in before["acquisitions"] if a["state"] == "running"]
            lab.check("old in-flight Acquisition is abandoned", old and all(a["state"] == "abandoned" for a in
                      after["acquisitions"] if a["id"] in old), after["acquisitions"])
    finally:
        gate.set()
        if child and child.poll() is None:
            child.kill()
            child.wait(10)
        fx.close()


def provenance():
    paths = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "src").rglob("*.sql"))
    source = {str(p.relative_to(ROOT)): sha256(p.read_bytes()) for p in paths}
    tests = {str(p.relative_to(ROOT)): sha256(p.read_bytes()) for p in sorted((ROOT / "tests").glob("issue31_*.py"))}
    return {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "python": sys.version, "source_hashes": source, "test_hashes": tests}


TESTS = [test_one_request_cannot_deliver_static_body, test_real_browser_inputs_and_published_bytes,
         test_necessary_script_500_blocks_version, test_necessary_script_byte_cap_blocks_version,
         test_worker_takeover_preserves_consumed_requests]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args()
    if not args.run.replace("-", "").replace("_", "").isalnum():
        parser.error("--run must contain only letters, digits, '-' and '_'")
    lab = Lab("issue31-browser-budget")
    lab.artifacts = ROOT / "artifacts" / "issue31-browser-budget" / args.run
    lab.artifacts.mkdir(parents=True, exist_ok=True)
    start = provenance()
    lab.save("failure-boundaries.txt", __doc__)
    lab.save("environment.json", {**start, "reproduce": ".venv/bin/python tests/issue31_browser_budget.py --run " + args.run,
             "prerequisites": "root .venv; Docker; postgres:17-alpine; seal-minio:RELEASE.2025-10-15T17-29-55Z",
             "isolation": "random-port tmpfs PostgreSQL/MinIO; random credentials; dedicated bucket; loopback fixtures; no .env.local"})
    print("artifacts:", lab.artifacts, flush=True)
    try:
        lab.start()
        result = lab.cli("init", label="init isolated database")
        if result.returncode:
            raise RuntimeError("isolated init failed: " + lab.redact(result.stderr[-1500:]))
        run_tests(lab, [t for t in TESTS if not args.only or t.__name__ in args.only])
    finally:
        lab.stop()
        end = provenance()
        summary = {s: sum(r["status"] == s for r in lab.results) for s in ("PASS", "FAIL", "NOT VERIFIED")}
        lab.save("provenance-end.json", end)
        lab.save("summary.json", {"summary": summary, "results": lab.results,
                                  "source_changed_during_run": start["source_hashes"] != end["source_hashes"]})
        leaked = []
        for p in lab.artifacts.rglob("*"):
            if p.is_file() and any(s.encode() in p.read_bytes() for s in lab.secrets if s):
                leaked.append(str(p.relative_to(lab.artifacts)))
        lab.save("secret-scan.json", {"pass": not leaked, "secret_values_checked": len(lab.secrets), "leaking_files": leaked})
        hashes = {str(p.relative_to(lab.artifacts)): sha256(p.read_bytes()) for p in sorted(lab.artifacts.rglob("*")) if p.is_file()}
        lab.save("artifact-sha256.json", hashes)
        print(json.dumps({**summary, "secret_scan_pass": not leaked}, ensure_ascii=False), flush=True)
    return int(bool(summary["FAIL"] or summary["NOT VERIFIED"] or leaked))


if __name__ == "__main__":
    raise SystemExit(main())
