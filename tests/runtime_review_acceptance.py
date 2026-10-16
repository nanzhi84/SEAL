"""Fresh end-to-end acceptance for PR #29's two runtime/PDF P2 findings.

Run from root: uv run --no-sync python tests/runtime_review_acceptance.py
Baseline: add --source-root work/runtime-review-baseline-src --label baseline
Prerequisites: frozen dependencies, Docker postgres:17-alpine + seal-minio image,
installed Chromium and headless shell, Tesseract eng, and macOS cp -cR/Arial.
All processes, sources, PostgreSQL/MinIO containers and browser copies are local
and disposable. No private SEAL functions or mocks are used.

Failure boundaries fixed before implementation: invisible actual browser/driver
identity; retry silently changes deployment; irrelevant browser noise pollutes
HTTP/PDF; HTML-only configuration creates PDF history; mixed-source HTML loses
its semantics; true native/OCR semantic changes stop versioning; failed browser
startup advances current success. Every check is an external behavior contract.
"""

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

from runtime_review_support import (
    ROOT,
    ReviewRuntime,
    adjust,
    pdf_bytes,
    per_url,
    sanitized_results,
    scanned_pdf,
    sha,
    tree_hash,
)


def rendered(rt):
    rt.fixture.html("/dom", "SERVER INPUT", script="<script>document.querySelector('h1').textContent='REAL JAVASCRIPT DOM OUTPUT';</script>")
    config = rt.config("dd-017", ["/dom"])
    adjust(config, parser={"mode": "rendered_dom"})
    rt.collect(config)
    first = rt.versions("dd-017")[-1]
    output = rt.archive(first)
    rt.check("rendered collection actually executes source JavaScript", "REAL JAVASCRIPT DOM OUTPUT" in str(output["result"]["markdown"]))
    traces = [x for x in rt.executions() if x["render_launch"]]
    if not traces:
        raise AssertionError("no real launch through the disposable Chromium installation")
    parents = [x["parent"] for x in traces]
    active = "patchright" if any("/patchright/" in x for x in parents) else "playwright"
    rt.check("real Chromium parent identifies one installed browser driver", all("/" + active + "/" in x for x in parents), parents)
    rt.save("actual-browser-driver.json", {"active_driver": active, "executions": traces})
    run = rt.query("SELECT * FROM seal.run WHERE source_id='dd-017' ORDER BY created_at DESC LIMIT 1")[0]
    manifest = json.dumps(run["parser_manifest"], sort_keys=True)
    rt.check("Run records the actual driver distribution version", active in manifest and importlib.metadata.version(active) in manifest, run["parser_manifest"])
    rt.collect(config)
    rt.check("unchanged rendered runtime reuses successful V1", len(rt.versions("dd-017")) == 1)

    unused = "patchright" if active == "playwright" else "playwright"
    unused_env, unused_info = rt.package_variant(unused)
    before = rt.versions("dd-017")
    rt.collect(config, env=unused_env)
    rt.check("unselected browser driver distribution is irrelevant noise", len(rt.versions("dd-017")) == len(before))
    active_env, active_info = rt.package_variant(active)
    before = rt.versions("dd-017")
    rt.collect(config, env=active_env)
    after = rt.versions("dd-017")
    rt.check("selected driver deployment change versions identical rendered input", len(after) == len(before) + 1 and after[-1]["parser_fingerprint"] != before[-1]["parser_fingerprint"] and after[-1]["body_hash"] == before[-1]["body_hash"])
    active_run = rt.query("SELECT * FROM seal.run WHERE source_id='dd-017' ORDER BY created_at DESC LIMIT 1")[0]
    rt.check("selected driver upgrade is traceable in immutable Run manifest", active_info["deployment"] in json.dumps(active_run["parser_manifest"]))
    rt.collect(config)

    before = rt.versions("dd-017")
    before_trace = len(rt.executions())
    rt.browser_variant("B")
    rt.collect(config)
    after = rt.versions("dd-017")
    new_trace = rt.executions()[before_trace:]
    rt.check("new Run actually executes changed browser installation", any(x["variant"] == "B" and x["render_launch"] for x in new_trace))
    rt.check("changed installed Chromium identity versions unchanged DOM", len(after) == len(before) + 1 and after[-1]["body_hash"] == before[-1]["body_hash"] and after[-1]["parser_fingerprint"] != before[-1]["parser_fingerprint"], {"before_count": len(before), "after_count": len(after)})
    later = rt.query("SELECT * FROM seal.run WHERE source_id='dd-017' ORDER BY created_at DESC LIMIT 1")[0]
    rt.check("Run manifest makes installed Chromium deployment identity traceable", later["parser_manifest"] != run["parser_manifest"], {"before": run["parser_manifest"], "after": later["parser_manifest"]})
    rt.collect(config)
    rt.check("stable changed Chromium never repeatedly versions", len(rt.versions("dd-017")) == len(after))
    rt.save("driver-deployments.json", {"selected": active_info, "unselected": unused_info, "browser_hashes": {str(path): sha(path) for path, _ in rt.browser_paths}})
    return config, active_env


def retry(rt, active_env):
    for source, change, env in (("dd-021", "browser", rt.env), ("dd-022", "driver", active_env)):
        rt.browser_variant("A")
        run = rt.interrupt_dom(source, rt.env)
        first = rt.versions(source)[0]
        before_trace = len(rt.executions())
        if change == "browser":
            rt.browser_variant("B")
        rt.cli("worker", "--once", env=env, success=False, timeout=90)
        after = rt.query("SELECT * FROM seal.run WHERE id=%s", (run["id"],))[0]
        versions = rt.versions(source)
        resumed = [x for x in rt.executions()[before_trace:] if x["render_launch"]]
        pinned = all(x["variant"] == "A" for x in resumed) and (change != "driver" or all("-deployment/" not in x["parent"] for x in resumed))
        rejected = len(versions) == 1 and bool(after["errors"]) and after["status"] != "complete"
        rt.check(change + " change cannot silently switch a frozen Run on retry", rejected or (after["status"] == "complete" and pinned), {"status": after["status"], "errors": after["errors"], "before": 1, "after": len(versions), "executions": resumed})
        rt.check(change + " retry keeps original Run manifest and prior success", after["parser_manifest"] == run["parser_manifest"] and versions[0]["id"] == first["id"])
        rt.save(source + "-retry.json", {"initial": run, "retried": after, "executions": resumed, "versions": versions, "observations": rt.observations(source)})


def pdf_and_http(rt, active_env):
    native = pdf_bytes("NATIVE PDF PUBLIC ACCEPTANCE TEXT")
    scan = scanned_pdf()
    rt.fixture.pages["/native.pdf"] = (200, "application/pdf", native)
    rt.fixture.pages["/scan.pdf"] = (200, "application/pdf", scan)
    rt.fixture.pages["/mixed.html"] = (200, "text/html", b"<html><body><main><p>MAIN OUTSIDE ARTICLE PUBLIC WORDS</p><article><h1>ARTICLE PUBLIC WORDS</h1><p>Enough public content for this actual mixed source HTML contract.</p></article></main></body></html>")
    ocr = {"languages": "eng", "dpi": 144, "timeout_seconds": 30, "max_pages": 2, "max_page_pixels": 10000000, "max_result_bytes": 2000000}
    config = rt.config("dd-032", ["/native.pdf", "/scan.pdf", "/mixed.html"], ocr=ocr)
    adjust(config, parser={"css_selector": "main", "max_depth": 0})
    rt.browser_variant("A")
    rt.collect(config)
    first = per_url(rt, "dd-032")
    native_url, scan_url, html_url = [rt.fixture.base + x for x in ("/native.pdf", "/scan.pdf", "/mixed.html")]
    rt.check("mixed Source obtains native PDF, real OCR and HTML successes", set(first) == {native_url, scan_url, html_url})
    for url, method, marker in ((native_url, "native", "NATIVE PDF"), (scan_url, "ocr", "SCAN RECEIPT")):
        archive = rt.archive(first[url][-1])
        actual_method = archive["result"]["metadata"]["pdf"]["method"]
        rt.check("actual PDF processing method " + method, actual_method == method or (method == "native" and actual_method == "text"), actual_method)
        rt.check("actual PDF text reaches stored CrawlResult " + method, marker in str(archive["result"]["markdown"]))
    before_trace = len(rt.executions())
    rt.browser_variant("B")
    rt.collect(config)
    after = per_url(rt, "dd-032")
    rt.check("browser installation change does not pollute HTTP, native PDF or OCR identities", all(len(after[url]) == len(first[url]) for url in first))
    rt.check("pure HTTP and PDF never launch Chromium", not any(x["render_launch"] for x in rt.executions()[before_trace:]))
    rt.collect(config, env=active_env)
    after = per_url(rt, "dd-032")
    rt.check("selected browser driver package change does not pollute HTTP or PDF identities", all(len(after[url]) == len(first[url]) for url in first))
    for parameter, value in (("word_count_threshold", 2), ("css_selector", "article")):
        before = per_url(rt, "dd-032")
        adjust(config, parser={parameter: value})
        rt.collect(config)
        after = per_url(rt, "dd-032")
        for url in (native_url, scan_url):
            observations = [row for row in rt.observations("dd-032") if row["resource_id"] == before[url][-1]["resource_id"]]
            rt.check(parameter + " cannot create a PDF semantic Version: " + url.rsplit("/", 1)[1], len(after[url]) == len(before[url]) and observations[-1]["version_id"] == before[url][-1]["id"], {"before": len(before[url]), "after": len(after[url]), "source_sha256": after[url][-1]["body_hash"], "observation": observations[-1]})
        rt.check(parameter + " still versions HTML in the same mixed Source", len(after[html_url]) == len(before[html_url]) + 1 and after[html_url][-1]["parser_fingerprint"] != before[html_url][-1]["parser_fingerprint"])
        rt.collect(config)
        rt.check(parameter + " stable repeat creates observations without repeat Versions", {url: len(rows) for url, rows in per_url(rt, "dd-032").items()} == {url: len(rows) for url, rows in after.items()})
    before = per_url(rt, "dd-032")
    ocr["dpi"] = 192
    adjust(config, ocr=ocr)
    rt.collect(config)
    after = per_url(rt, "dd-032")
    rt.check("genuine OCR dpi semantic change still versions unchanged scanned PDF", len(after[scan_url]) == len(before[scan_url]) + 1 and after[scan_url][-1]["body_hash"] == before[scan_url][-1]["body_hash"] and after[scan_url][-1]["parser_fingerprint"] != before[scan_url][-1]["parser_fingerprint"])
    rt.check("changed dpi really runs OCR successfully", rt.archive(after[scan_url][-1])["result"]["metadata"]["pdf"]["method"] == "ocr")
    deployment = rt.work / "pdf-contract-deployment"
    shutil.copytree(rt.deployment, deployment)
    replacements = 0
    for path in (deployment / "seal").glob("*.py"):
        text = path.read_text()
        if "crawl4ai_0_9_5_native_pages_v1" in text:
            replacements += 1
            path.write_text(text.replace("crawl4ai_0_9_5_native_pages_v1", "crawl4ai_0_9_5_native_pages_independent_review"))
    if replacements != 1:
        raise AssertionError("unable to identify the documented native PDF text contract deployment")
    env = dict(rt.env, PYTHONPATH=str(deployment))
    before = per_url(rt, "dd-032")
    rt.collect(config, env=env)
    after = per_url(rt, "dd-032")
    rt.check("real native PDF text contract deployment still versions identical PDF", len(after[native_url]) == len(before[native_url]) + 1 and after[native_url][-1]["body_hash"] == before[native_url][-1]["body_hash"] and after[native_url][-1]["parser_fingerprint"] != before[native_url][-1]["parser_fingerprint"])
    rt.collect(config, env=env)
    rt.check("stable text-contract deployment never repeatedly versions", len(per_url(rt, "dd-032")[native_url]) == len(after[native_url]))
    rt.save("pdf-inputs.json", {"native_sha256": hashlib.sha256(native).hexdigest(), "scan_sha256": hashlib.sha256(scan).hexdigest(), "scan_text_layer_empty": True, "first": first, "last": per_url(rt, "dd-032")})


def missing_browser(rt, config):
    before = rt.versions("dd-017")
    missing = rt.work / "empty-browser-installation"
    missing.mkdir()
    code, _ = rt.collect(config, env=dict(rt.env, PLAYWRIGHT_BROWSERS_PATH=str(missing)), success=False)
    rt.check("unavailable browser cannot advance successful rendered history", code != 0 and rt.versions("dd-017") == before)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--label", default="final")
    args = parser.parse_args()
    rt = ReviewRuntime(args.source_root, args.label)
    error = None
    try:
        rt.start()
        rt.browser_copy()
        rt.cli("init")
        config, active_env = rendered(rt)
        retry(rt, active_env)
        pdf_and_http(rt, active_env)
        missing_browser(rt, config)
        sanitized_results(rt, "final-storage-evidence.json", ("dd-017", "dd-021", "dd-022", "dd-032"))
    except BaseException as exc:
        error = str(exc)
        if rt.s3 is not None:
            rt.save("failed-state.json", rt.state())
    finally:
        stable = rt.initial_hash == tree_hash(rt.deployment)
        tests_stable = rt.test_hashes == {p.name: sha(p) for p in Path(__file__).parent.glob("runtime_review*.py")}
        rt.save("actual-browser-executions.json", rt.executions())
        rt.save("reproduction.json", {"command": "uv run --no-sync python tests/runtime_review_acceptance.py --source-root " + str(args.source_root) + " --label " + args.label, "source_root": str(args.source_root.resolve()), "source_sha256": rt.initial_hash, "source_stable": stable, "tests_sha256": {p.name: sha(p) for p in Path(__file__).parent.glob("runtime_review*.py")}, "dependencies": {name: importlib.metadata.version(name) for name in ("crawl4ai", "playwright", "patchright", "pypdf", "pypdfium2")}, "uv_lock_sha256": sha(ROOT / "uv.lock"), "environment": "Python 3.12; temporary PostgreSQL 17/MinIO containers and private bucket; loopback controlled sources; disposable Chromium installation and driver distribution copies; real native PDF and Tesseract eng OCR", "tesseract": subprocess.check_output(["tesseract", "--version"], text=True).splitlines()[0]})
        result = {"pass": error is None and stable and tests_stable and all(row["pass"] for row in rt.checks), "infrastructure_error": error, "source_stable": stable, "tests_stable": tests_stable, "assertions": len(rt.checks), "passed": sum(row["pass"] for row in rt.checks), "failed": [row["assertion"] for row in rt.checks if not row["pass"]]}
        rt.save("result.json", result)
        rt.close()
        print(json.dumps({"artifacts": str(rt.artifacts), **result}, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
