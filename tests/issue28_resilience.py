"""Runtime fault injection and concurrent public workflow acceptance for #28.

Faults are confined to subprocess signals, disposable deployment copies,
controlled HTTP fixtures and isolated database/object-store containers.
"""

import importlib.metadata
import importlib.util
import json
import os
import shutil
import signal
import threading
import time
import uuid

import psycopg
from issue28_runtime import ROOT, pdf_bytes


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("bounded wait expired")


def variant(rt, name, replacements=None, framework=False):
    """Model a deployment without modifying the checkout or its environment."""
    target = rt.work / name
    target.mkdir()
    shutil.copytree(ROOT / "src" / "seal", target / "seal")
    for path in (target / "seal").rglob("*.py"):
        text = path.read_text()
        for old, new in (replacements or {}).items():
            text = text.replace(old, new)
        path.write_text(text)
    if framework:
        package = importlib.util.find_spec("crawl4ai").origin
        shutil.copytree(os.path.dirname(package), target / "crawl4ai")
        info = importlib.metadata.distribution("crawl4ai")._path
        shutil.copytree(info, target / "crawl4ai-0.9.5.dist-info")
        for path in (
            target / "crawl4ai" / "__version__.py",
            target / "crawl4ai-0.9.5.dist-info" / "METADATA",
        ):
            path.write_text(path.read_text().replace("0.9.5", "0.9.5+issue28deployment"))
    env = dict(rt.env, PYTHONPATH=str(target))
    return env


def deployment_contracts(rt):
    rt.fixture.pages["/document.pdf"] = (
        200,
        "application/pdf",
        pdf_bytes("PDF BRAVO public text layer acceptance"),
    )
    pdf_config = rt.work / "dd-448.yaml"
    before = rt.versions("dd-448")
    changed = variant(
        rt,
        "pdf-contract-deployment",
        {"crawl4ai_0_9_5_native_pages_v1": "crawl4ai_0_9_5_native_pages_issue28_acceptance"},
    )
    rt.check(
        "PDF adapter deployment alone does not write versions", rt.versions("dd-448") == before
    )
    rt.collect(pdf_config, env=changed)
    after = rt.versions("dd-448")
    rt.check(
        "PDF text contract deployment creates one observed version",
        len(after) == len(before) + 1
        and after[-1]["body_hash"] == before[-1]["body_hash"]
        and after[-1]["parser_fingerprint"] != before[-1]["parser_fingerprint"],
    )
    rt.collect(pdf_config, env=changed)
    rt.check(
        "stable PDF text contract does not repeatedly version",
        len(rt.versions("dd-448")) == len(after),
    )
    rt.fixture.html("/framework", "FRAMEWORK STABLE")
    config = rt.config("dd-019", ["/framework"])
    rt.collect(config)
    first = rt.versions("dd-019")[-1]
    upgraded = variant(rt, "framework-deployment", framework=True)
    rt.collect(config, env=upgraded)
    second = rt.versions("dd-019")[-1]
    rt.check(
        "Crawl4AI deployment identity is a semantic fingerprint input",
        len(rt.versions("dd-019")) == 2
        and first["body_hash"] == second["body_hash"]
        and first["parser_fingerprint"] != second["parser_fingerprint"]
        and first["framework_version"] != second["framework_version"],
    )
    rt.save(
        "deployment-contracts.json",
        {"pdf_before": before, "pdf_after": after, "framework_versions": rt.versions("dd-019")},
    )


def representation_inputs(rt):
    rt.fixture.html("/stale-success", "LAST SUCCESSFUL CONTENT")
    stale_config = rt.config("dd-030", ["/stale-success"])
    rt.collect(stale_config)
    rt.fixture.html("/stale-success", "TEMPORARY FAILURE", status=503)
    rt.collect(stale_config, success=False)
    for source, path, body, expected in (
        ("dd-026", "/short.txt", b"Hello.", "Hello."),
        ("dd-027", "/literal.txt", b"<h1>This is literal text</h1>", "h1"),
    ):
        rt.fixture.pages[path] = (200, "text/plain; charset=utf-8", body)
        config = rt.config(source, [path])
        rt.collect(config)
        version = rt.versions(source)[-1]
        markdown = rt.archive(version)["result"]["markdown"]
        rt.check(
            "short and HTML-looking plain text retains literal content " + source,
            expected in str(markdown),
        )
        rt.collect(config)
        rt.check(
            "stable plain text edge case reuses version " + source, len(rt.versions(source)) == 1
        )
    rt.fixture.html("/charset", "CHARSET café meaningful content")
    body = rt.fixture.pages["/charset"][2]
    config = rt.config("dd-023", ["/charset"])
    rt.collect(config)
    first = rt.versions("dd-023")[-1]
    rt.fixture.pages["/charset"] = (200, "text/html; charset=iso-8859-1", body)
    rt.collect(config)
    versions = rt.versions("dd-023")
    rt.check(
        "same HTTP bytes with changed effective charset produce new representation",
        len(versions) == 2
        and rt.archive(first)["result"]["markdown"]
        != rt.archive(versions[-1])["result"]["markdown"],
    )

    rt.fixture.html("/base-a/page", "REDIRECT BASE INPUT", links=("relative-child",))
    body = rt.fixture.pages["/base-a/page"][2]
    rt.fixture.pages["/base-b/page"] = (200, "text/html; charset=utf-8", body)
    rt.fixture.pages["/redirect"] = (302, "text/plain", b"", {"Location": "/base-a/page"})
    config = rt.config(
        "dd-024",
        ["/redirect"],
        crawl4ai={
            "mode": "http",
            "max_depth": 0,
            "page_timeout_ms": 10000,
            "word_count_threshold": 1,
        },
    )
    rt.collect(config)
    first = rt.versions("dd-024")[-1]
    rt.fixture.pages["/redirect"] = (302, "text/plain", b"", {"Location": "/base-b/page"})
    rt.collect(config)
    versions = rt.versions("dd-024")
    rt.check(
        "redirect effective base is part of observed source representation",
        len(versions) == 2 and rt.archive(first)["result"] != rt.archive(versions[-1])["result"],
    )
    rt.save(
        "representation-inputs.json",
        {
            x: {"versions": rt.versions(x), "observations": rt.observations(x)}
            for x in ("dd-023", "dd-024")
        },
    )


def interrupted_run(rt, source_id, stem):
    rt.fixture.html("/" + stem + "-a", "RETRY FIRST SUCCESS")
    rt.fixture.html("/" + stem + "-b", "RETRY SECOND SUCCESS")
    config = rt.config(source_id, ["/" + stem + "-a", "/" + stem + "-b"])
    blocked = threading.Event()
    rt.fixture.block["/" + stem + "-b"] = blocked
    proc = rt.spawn("collect", config, "--no-progress")
    wait_for(lambda: len(rt.versions(source_id)) == 1 and "/" + stem + "-b" in rt.fixture.requests)
    proc.send_signal(signal.SIGINT)
    proc.wait(20)
    blocked.set()
    del rt.fixture.block["/" + stem + "-b"]
    run = rt.query(
        "SELECT * FROM seal.run WHERE source_id=%s ORDER BY created_at DESC LIMIT 1", (source_id,)
    )[0]
    rt.check("user interruption preserves retryable run " + source_id, run["status"] == "retryable")
    return run


def concurrency_and_retry(rt):
    from issue28_acceptance import items, snapshot

    for path in ("/concurrent-a", "/concurrent-b"):
        rt.fixture.html(path, "CONCURRENT OLD")
    config = rt.config("dd-020", ["/concurrent-a", "/concurrent-b"])
    rt.collect(config)
    old = rt.versions("dd-020")
    for path in ("/concurrent-a", "/concurrent-b"):
        rt.fixture.html(path, "CONCURRENT NEW")
    block = threading.Event()
    rt.fixture.block["/concurrent-b"] = block
    proc = rt.spawn("collect", config, "--no-progress")
    wait_for(lambda: len(rt.versions("dd-020")) == 3)
    _, captured = snapshot(rt)
    sid = captured["snapshot_id"]
    frozen = items(rt, sid)
    mid = [x for x in frozen if x["source_id"] == "dd-020"]
    current = {
        x["url"]: x["current_version_id"]
        for x in rt.query(
            "SELECT r.url,r.current_version_id FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id WHERE f.source_id='dd-020'"
        )
    }
    rt.check(
        "snapshot concurrent with collection freezes coherent committed resource view",
        {x["url"]: x["version_id"] for x in mid} == current and len(mid) == 2,
    )
    block.set()
    del rt.fixture.block["/concurrent-b"]
    proc.wait(30)
    rt.check(
        "concurrent collection completes both new versions",
        len(rt.versions("dd-020")) == 4 and proc.returncode == 0,
    )
    rt.check("later version commits do not drift sealed snapshot", items(rt, sid) == frozen)
    cursors = rt.query("SELECT cursor FROM seal.version ORDER BY cursor")
    rt.check(
        "global version cursor remains unique and increasing",
        len({x["cursor"] for x in cursors}) == len(cursors)
        and all(a["cursor"] < b["cursor"] for a, b in zip(cursors, cursors[1:], strict=False)),
    )
    rt.save(
        "concurrency.json",
        {
            "before": old,
            "during_snapshot": captured,
            "frozen": frozen,
            "after": rt.versions("dd-020"),
            "global_cursors": cursors,
        },
    )

    watermark = rt.query("SELECT coalesce(max(cursor),0) AS n FROM seal.version")[0]["n"]
    processes = []
    for source in ("dd-028", "dd-029"):
        paths = ["/" + source + "/" + str(index) for index in range(3)]
        for path in paths:
            rt.fixture.html(path, "PARALLEL COMMIT " + source)
        cfg = rt.config(source, paths, concurrency=2)
        processes.append(rt.spawn("collect", cfg, "--no-progress"))
    delivered = []
    while any(proc.poll() is None for proc in processes):
        rows = rt.query(
            "SELECT id,cursor FROM seal.version_delta WHERE cursor>%s ORDER BY cursor", (watermark,)
        )
        delivered.extend(rows)
        if rows:
            watermark = rows[-1]["cursor"]
        time.sleep(0.05)
    rows = rt.query(
        "SELECT id,cursor FROM seal.version_delta WHERE cursor>%s ORDER BY cursor", (watermark,)
    )
    delivered.extend(rows)
    expected = rt.versions("dd-028") + rt.versions("dd-029")
    rt.check(
        "concurrent sources remain visible exactly once to advancing global cursor consumer",
        len(expected) == 6
        and {x["id"] for x in delivered} == {x["id"] for x in expected}
        and len({x["id"] for x in delivered}) == len(delivered)
        and all(proc.returncode == 0 for proc in processes),
    )
    rt.save(
        "incremental-cursor-consumption.json", {"observed_pages": delivered, "expected": expected}
    )

    run = interrupted_run(rt, "dd-021", "retry")
    first = rt.versions("dd-021")[0]
    rt.cli("worker", "--once", timeout=60)
    final = rt.query("SELECT * FROM seal.run WHERE id=%s", (run["id"],))[0]
    observations, versions = rt.observations("dd-021"), rt.versions("dd-021")
    rt.check(
        "retry completes original run without duplicate delivery or version",
        final["status"] == "complete"
        and len(versions) == 2
        and len([x for x in observations if x["resource_id"] == first["resource_id"]]) == 1
        and len({x["delivery_key"] for x in observations}) == len(observations),
    )
    rt.check(
        "retry keeps one frozen parser manifest",
        final["parser_manifest"] == run["parser_manifest"]
        and all(x["parser_fingerprint"] == first["parser_fingerprint"] for x in observations),
    )

    changed_run = interrupted_run(rt, "dd-022", "changed-retry")
    before = rt.versions("dd-022")
    deployed = variant(rt, "retry-new-framework", framework=True)
    rt.cli("worker", "--once", env=deployed, success=False, timeout=60)
    later = rt.query("SELECT * FROM seal.run WHERE id=%s", (changed_run["id"],))[0]
    new_obs = rt.observations("dd-022")
    rt.check(
        "retry after deployment never silently switches pinned parser",
        later["parser_manifest"] == changed_run["parser_manifest"]
        and all(
            x.get("parser_fingerprint") in (None, before[0]["parser_fingerprint"]) for x in new_obs
        ),
    )
    rt.check(
        "incompatible deployment is rejected or resumes pinned semantics",
        later["status"] == "complete" or any("parser" in str(x) for x in later["errors"]),
    )
    rt.save(
        "retries.json",
        {
            "initial_run": run,
            "resumed_run": final,
            "versions": versions,
            "observations": observations,
            "deployment_interrupted_run": changed_run,
            "deployment_resumed_run": later,
            "deployment_observations": new_obs,
        },
    )


def storage_atomicity(rt):
    rt.fixture.html("/atomic", "ATOMIC ORIGINAL")
    config = rt.config("dd-025", ["/atomic"])
    rt.collect(config)
    first = rt.versions("dd-025")[0]
    with rt.db() as conn:
        conn.execute("""CREATE FUNCTION seal.issue28_fail_version() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.resource_id = TG_ARGV[0] THEN RAISE EXCEPTION 'issue28 injected storage failure'; END IF;
            RETURN NEW; END $$""")
        conn.execute(
            "CREATE TRIGGER issue28_storage_failure BEFORE INSERT ON seal.version FOR EACH ROW "
            "EXECUTE FUNCTION seal.issue28_fail_version('" + first["resource_id"] + "')"
        )
    before = rt.observations("dd-025")
    rt.fixture.html("/atomic", "ATOMIC CHANGED")
    rt.collect(config, success=False)
    failed = [x for x in rt.observations("dd-025") if x["id"] not in {row["id"] for row in before}]
    rt.check(
        "storage failure rolls back observation/version/current-pointer unit",
        len(rt.versions("dd-025")) == 1
        and all(x["version_id"] is None and x["outcome"] != "parsed" for x in failed)
        and rt.query(
            "SELECT current_version_id FROM seal.resource WHERE id=%s", (first["resource_id"],)
        )[0]["current_version_id"]
        == first["id"],
    )
    with rt.db() as conn:
        conn.execute("DROP TRIGGER issue28_storage_failure ON seal.version")
        conn.execute("DROP FUNCTION seal.issue28_fail_version()")
    rt.collect(config)
    rt.check(
        "recovery commits exactly one next version after storage failure",
        len(rt.versions("dd-025")) == 2 and rt.versions("dd-025")[-1]["sequence"] == 2,
    )
    duplicate = dict(rt.versions("dd-025")[-1])
    duplicate.pop("url")
    duplicate.update(
        id=str(uuid.uuid4()),
        sequence=3,
        cursor=rt.query("SELECT max(cursor)+1 AS n FROM seal.version")[0]["n"],
    )
    rejected = False
    try:
        with rt.db() as conn:
            conn.execute(
                "INSERT INTO seal.version SELECT * FROM json_populate_record(NULL::seal.version,%s::json)",
                (json.dumps(duplicate, default=str),),
            )
    except psycopg.errors.UniqueViolation:
        rejected = True
    rt.check(
        "database forbids two versions created by one observation",
        rejected and len(rt.versions("dd-025")) == 2,
    )
    rt.save(
        "storage-atomicity.json",
        {
            "original": first,
            "after_failure_observations": failed,
            "recovered_versions": rt.versions("dd-025"),
        },
    )
