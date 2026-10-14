"""Actual collector pause plus concurrent database commit/publication acceptance."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import yaml
from issue20_fixture import html
from issue23_contract import path, stable
from issue23_lab import ROOT, write
from psycopg.types.json import Jsonb


def in_flight(lab, data):
    lab.origin.set("/active-index", html("Active collection", "Run must not finish before snapshot.", ("/active-before", "/active-slow")))
    lab.origin.set("/active-before", html("Committed sibling", "This success must be in the snapshot."))
    lab.origin.set("/active-slow", html("Uncommitted sibling", "This later success must not enter the snapshot."), delay=8)
    cfg = dict(id="dd-006", entry_urls=[lab.origin.base + "/active-index"],
               allowed_hosts=["127.0.0.1"], allowed_path_prefixes=["/active-index", "/active-before", "/active-slow"], archive_approved=True,
               scope="Issue23 isolated in-flight collection", robots=False, concurrency=1, delay=0.,
               budget=dict(requests=10, seconds=45, response_bytes=1048576),
               crawl4ai=dict(mode="http", max_depth=1, word_count_threshold=1))
    cfg_path = lab.work / "dd-006.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    proc = subprocess.Popen([sys.executable, "-m", "seal.cli", "collect", str(cfg_path), "--no-progress"],
                            cwd=ROOT, env=lab.admin_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        lab.origin.wait("/active-slow")
        current = lab.sql("SELECT r.url,r.current_version_id FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id "
                          "WHERE f.source_id='dd-006' ORDER BY r.url")
        lab.check("real collection paused with committed and not-yet-successful siblings", proc.poll() is None
                  and any(r["url"].endswith("/active-before") and r["current_version_id"] for r in current)
                  and not any(r["url"].endswith("/active-slow") and r["current_version_id"] for r in current), current=current)
        start = time.monotonic()
        ident = lab.create("in-flight")
        elapsed = time.monotonic() - start
        member_urls = [r["url"] for r in lab.mapping(ident)["items"] if r["source_id"] == "dd-006"]
        lab.check("snapshot creation does not wait for in-flight run", proc.poll() is None and elapsed < 5, elapsed=elapsed)
        lab.check("in-flight snapshot contains only committed successes", set(member_urls) == {
                  lab.origin.base + "/active-index", lab.origin.base + "/active-before"}, urls=member_urls)
        stdout, stderr = proc.communicate(timeout=60)
        lab.commands.append(dict(argv=["seal", "collect", "dd-006.yaml", "--no-progress"],
                                 exit=proc.returncode, stdout=lab.clean(stdout), stderr=lab.clean(stderr)))
        lab.check("collector eventually commits slow success without changing frozen snapshot", proc.returncode in (0, 1)
                  and len(lab.sql("SELECT r.id FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id "
                                  "WHERE f.source_id='dd-006' AND r.current_version_id IS NOT NULL")) == 3
                  and [r["url"] for r in lab.mapping(ident)["items"] if r["source_id"] == "dd-006"] == member_urls)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=15)


def atomic_view(lab, data):
    data.source("dd-007", "https://fixture.example.org/atomic-alpha")
    a, b = [], []
    for n in range(2):
        a.append(dict(data.success("dd-007", f"atomic-{n}", data.envelope(f"Atomic Alpha {n}"))))
        b.append(dict(data.success("dd-007", f"atomic-{n}", data.envelope(f"Atomic Beta {n}"))))
    baseline_config = lab.sql("SELECT config FROM seal.source WHERE id='dd-007'")[0]["config"]
    phases = {
        "alpha": ([r["version"] for r in a], {**baseline_config, "entry_urls": ["https://fixture.example.org/atomic-alpha"]}),
        "beta": ([r["version"] for r in b], {**baseline_config, "entry_urls": ["https://fixture.example.org/atomic-beta"]})}
    lab.sql("UPDATE seal.source SET config=%s WHERE id='dd-007'", (Jsonb(phases["beta"][1]),))
    failures, commits, evidence = [], [], []
    active = threading.Event()

    def mutate():
        try:
            for n in range(30):
                phase = "alpha" if n % 2 == 0 else "beta"
                versions, config = phases[phase]
                with psycopg.connect(lab.dsn) as conn:
                    conn.execute("UPDATE seal.source SET config=%s WHERE id='dd-007'", (Jsonb(config),))
                    conn.execute("UPDATE seal.resource SET current_version_id=%s WHERE id='atomic-0'", (versions[0],))
                    active.set()
                    time.sleep(.08)
                    conn.execute("UPDATE seal.resource SET current_version_id=%s WHERE id='atomic-1'", (versions[1],))
                    time.sleep(.04)
                commits.append(phase)
        except Exception as exc:
            failures.append(type(exc).__name__)

    thread = threading.Thread(target=mutate)
    thread.start()
    active.wait(timeout=10)
    try:
        for n in range(8):
            ident = lab.create("atomic-" + str(n))
            mapping = lab.mapping(ident)
            source = next(r for r in mapping["sources"] if r["source_id"] == "dd-007")
            versions = [r["version_id"] for r in mapping["items"] if r["source_id"] == "dd-007"]
            phase = source["entry_urls"][0].rsplit("-", 1)[1]
            expected_versions = phases[phase][0]
            lab.check("consistent source+two-version snapshot during concurrent commit", versions == expected_versions,
                      snapshot_id=ident, source_entry=source["entry_urls"], version_ids=versions, expected=expected_versions)
            evidence.append(dict(snapshot_id=ident, phase=phase, version_ids=versions))
    finally:
        thread.join(timeout=15)
    lab.check("atomic fixture performed real overlapping commits", not thread.is_alive() and not failures and len(commits) == 30,
              commits=len(commits), failures=failures)
    write(lab.artifacts / "consistent-concurrent-create.json", evidence)


def worker_path(lab, data):
    """Queue and consume a real collect job; publication remains an explicit operation."""
    lab.origin.set("/worker-index", html("Worker successful content", "A queued collector must not create or publish snapshots."))
    cfg = dict(id="dd-009", entry_urls=[lab.origin.base + "/worker-index"], allowed_hosts=["127.0.0.1"],
               allowed_path_prefixes=["/worker-index"], archive_approved=True,
               scope="Issue23 isolated actual persistent worker path", robots=False, concurrency=1, delay=0.,
               budget=dict(requests=5, seconds=45, response_bytes=1048576),
               crawl4ai=dict(mode="http", max_depth=0, word_count_threshold=1))
    cfg_path = lab.work / "dd-009.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    count_before = lab.sql("SELECT count(*) AS n FROM seal.snapshot")[0]["n"]
    pointer_before, audit_before = lab.pointer(), lab.audit()
    lab.cli("collect", cfg_path, "--enqueue")
    lab.check("enqueue creates no snapshot or publication", lab.sql("SELECT count(*) AS n FROM seal.snapshot")[0]["n"] == count_before
              and lab.pointer() == pointer_before and lab.audit() == audit_before)
    lab.cli("worker", "--once", timeout=120)
    rows = lab.sql("SELECT r.url,r.current_version_id,v.result_ref FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id "
                   "JOIN seal.version v ON v.id=r.current_version_id WHERE f.source_id='dd-009'")
    lab.check("real worker executes queued collection and commits archived successful content", len(rows) == 1
              and rows[0]["url"] == lab.origin.base + "/worker-index" and bool(lab.body(rows[0]["result_ref"])))
    lab.check("worker commits no implicit snapshot or publication", lab.sql("SELECT count(*) AS n FROM seal.snapshot")[0]["n"] == count_before
              and lab.pointer() == pointer_before and lab.audit() == audit_before)
    write(lab.artifacts / "queued-worker.json", dict(command=["seal collect dd-009.yaml --enqueue", "seal worker --once"],
          versions=rows, snapshot_count_before=count_before,
          snapshot_count_after=lab.sql("SELECT count(*) AS n FROM seal.snapshot")[0]["n"],
          pointer_before=pointer_before, pointer_after=lab.pointer(), publication_records=lab.audit()))


def publication(lab, data):
    lab.publish(data.a)
    before_records = lab.audit()
    received, failures = [], []
    stop = threading.Event()

    def read_requests():
        try:
            while not stop.is_set():
                status, _, value, _, _ = lab.request(path(data.entries["dd-001"], limit=100))
                received.append(dict(status=status, content=stable(value) if isinstance(value, list) else value))
                time.sleep(.03)
        except Exception as exc:
            failures.append(type(exc).__name__)

    watcher = threading.Thread(target=read_requests)
    watcher.start()
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda ident: lab.cli("snapshot", "publish", ident),
                                    [data.b, data.a] * 6))
        lab.check("all concurrent publications have explicit successful receipt", all(p.returncode == 0 for p in results))
    finally:
        stop.set()
        watcher.join(timeout=10)
    audit = lab.audit()
    transitions = audit[len(before_records):]
    previous = before_records[-1]["after_snapshot_id"]
    chain = True
    for row in transitions:
        chain = chain and row["before_snapshot_id"] == previous and row["after_snapshot_id"] in (data.a, data.b)
        previous = row["after_snapshot_id"]
    lab.check("concurrent publications serialize into consistent pointer/audit chain", chain and previous == lab.pointer()
              and all(r["published_at"] for r in transitions), transitions=transitions, pointer=lab.pointer())
    lab.check("every new HTTP request during publication belongs wholly to A or B", received and not failures
              and all(r["status"] == 200 and r["content"] in (data.a_native, data.b_native) for r in received),
              requests=len(received), failures=failures)
    current = lab.pointer()
    before = lab.audit()
    lab.publish(current)
    lab.check("repeat publication of current snapshot is idempotent without false switch record", lab.pointer() == current and lab.audit() == before)
    for invalid in ("not-a-snapshot", "00000000-0000-0000-0000-000000000000"):
        result = lab.cli("snapshot", "publish", invalid, expected=(1, 2))
        lab.check("invalid publish leaves pointer and log intact with explicit safe error", result.returncode != 0
                  and bool(result.stderr or result.stdout) and "Traceback" not in result.stderr
                  and lab.pointer() == current and lab.audit() == before)
    write(lab.artifacts / "http-during-publication.json", received)
