"""Independent Issue 31 business scenarios; product access uses CLI, SQL, S3 and HTTP."""
# ruff: noqa: F403,F405
from issue31_common import *

# ============================================================ network refusal / cancellation / stale


def test_failure_network_refused_entry(lab):
    """Entry on a refused port: explicit acquisition failure, no version."""
    port = free_port()
    cfg = base_source("dd-183", type("F", (), {"base": f"http://127.0.0.1:{port}"})(), ["/refused.pdf"])
    result, receipt, _ = collect(lab, "dd-183", cfg, label="collect refused")
    lab.save("receipts/dd-183-refused.json", receipt or result.stdout + result.stderr)
    acq = lab.q("SELECT state, outcome, error, archive_ref FROM seal.acquisition WHERE source_id='dd-183'")
    versions = lab.q("SELECT v.id FROM seal.version v JOIN seal.resource r ON r.id=v.resource_id JOIN seal.frontier f ON f.id=r.frontier_id WHERE f.source_id='dd-183'")
    lab.check("refused acquisition recorded as failed", acq and all(a["state"] == "failed" and a["archive_ref"] is None for a in acq), acq)
    lab.check("no version", not versions, versions)
    status = (receipt or {}).get("status")
    lab.check("run not reported as clean success", result.returncode != 0 or counts(receipt).get("request_failed", 0) >= 1 or status not in ("complete",),
              {"exit": result.returncode, "status": status, "counts": counts(receipt)})


def _cancel(lab, sid, sig):
    fx = Fixture()
    c = S["content"]
    fx.put("/c/fast.html", structure_detail(f"Fast page {sid}"))
    fx.put("/c/slow.pdf", c.b["vol1.pdf"], "application/pdf")
    fx.hang["/c/slow.pdf"] = __import__("threading").Event()
    cfg = base_source(sid, fx, ["/c/fast.html", "/c/slow.pdf"], concurrency=1)
    path = lab.source_file(sid, cfg)
    child = lab.popen("collect", path, "--no-progress")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not fx.hits.get("/c/slow.pdf"):
        if child.poll() is not None:
            break
        time.sleep(0.2)
    lab.check("collect reached the slow request", fx.hits.get("/c/slow.pdf"), {"hits": fx.hits, "log": Path(child.logfile).read_text()[-1500:]})
    time.sleep(1.0)
    child.send_signal(sig)
    try:
        code = child.wait(90)
    except subprocess.TimeoutExpired:
        child.kill()
        code = "timeout"
    fx.hang["/c/slow.pdf"].set()
    log = Path(child.logfile).read_text()
    lab.save(f"evidence/cancel-{sid}.log", log[-6000:])
    runs = lab.q("SELECT id, status, errors FROM seal.run WHERE source_id=%s", (sid,))
    acq = lab.q("SELECT request_url, state, outcome FROM seal.acquisition WHERE source_id=%s", (sid,))
    slow_url = fx.base + "/c/slow.pdf"
    versions = lab.q("SELECT v.id FROM seal.version v JOIN seal.resource r ON r.id=v.resource_id WHERE r.external_key=%s", (slow_url,))
    lab.save(f"evidence/cancel-{sid}.json", {"exit": code, "runs": runs, "acquisitions": acq})
    lab.check("collect exits non-zero after signal", code not in (0, "timeout"), code)
    lab.check("run is not complete (cancelled/failed)", runs and all(r["status"] not in ("complete", "running", "pending") for r in runs), runs)
    lab.check("no acquisition left running", all(a["state"] != "running" for a in acq), acq)
    lab.check("in-flight acquisition cancelled", any(a["request_url"] == slow_url and a["state"] in ("cancelled", "abandoned", "failed") for a in acq), acq)
    lab.check("no success version for cancelled document", not versions, versions)
    receipt = parse_json_tail(log)
    lab.check("cancellation reported explicitly", any(word in json.dumps(receipt or log).lower() for word in ("cancel", "interrupt")),
              log[-800:] if receipt is None or isinstance(receipt, str) else receipt.get("status"))
    fx.close()


def test_cancel_sigint(lab):
    """SIGINT during a running collect: explicit cancellation, no fake success."""
    _cancel(lab, "dd-066", signal.SIGINT)


def test_cancel_sigterm(lab):
    """SIGTERM during a running collect: explicit cancellation, no fake success."""
    _cancel(lab, "dd-102", signal.SIGTERM)


def test_stale_attempt_takeover(lab):
    """SIGKILLed collect leaves a stale attempt; takeover abandons it; stale writes impossible."""
    sid = "dd-155"
    fx = Fixture()
    fx.put("/s/doc.pdf", S["content"].b["vol2.pdf"], "application/pdf")
    gate = __import__("threading").Event()
    fx.hang["/s/doc.pdf"] = gate
    cfg = base_source(sid, fx, ["/s/doc.pdf"], budget={"requests": 50, "seconds": 20, "response_bytes": 10485760, "documents": 100})
    path = lab.source_file(sid, cfg)
    child = lab.popen("collect", path, "--no-progress")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not fx.hits.get("/s/doc.pdf") and child.poll() is None:
        time.sleep(0.2)
    lab.check("collect reached the request", fx.hits.get("/s/doc.pdf"), Path(child.logfile).read_text()[-1500:])
    child.send_signal(signal.SIGKILL)
    child.wait(30)
    stale = lab.q("SELECT id, status, attempt_epoch FROM seal.run WHERE source_id=%s", (sid,))
    stale_acq = lab.q("SELECT id, state, attempt_epoch FROM seal.acquisition WHERE source_id=%s", (sid,))
    lab.save("evidence/stale-before.json", {"runs": stale, "acquisitions": stale_acq})
    # The old request keeps its Event reference; new requests answer immediately.
    # Do not release the old response until a new attempt actually takes ownership.
    fx.hang.pop("/s/doc.pdf", None)
    outcome = None
    for attempt in range(6):
        result, receipt, _ = collect(lab, sid, cfg, label=f"stale takeover attempt {attempt}")
        outcome = {"exit": result.returncode, "receipt": receipt, "stderr": result.stderr[-800:]}
        acq = lab.q("SELECT id, state, attempt_epoch, run_id FROM seal.acquisition WHERE source_id=%s ORDER BY started_at", (sid,))
        if result.returncode == 0 and all(a["state"] != "running" for a in acq):
            break
        time.sleep(10)
    gate.set()
    time.sleep(2)
    after_runs = lab.q("SELECT id, status, attempt_epoch FROM seal.run WHERE source_id=%s ORDER BY created_at", (sid,))
    after_acq = lab.q("SELECT id, state, attempt_epoch, run_id FROM seal.acquisition WHERE source_id=%s ORDER BY started_at", (sid,))
    obs = lab.q("""SELECT o.outcome, o.attempt_epoch, o.run_id FROM seal.observation o JOIN seal.resource r ON r.id=o.resource_id
                   WHERE r.external_key=%s""", (fx.base + "/s/doc.pdf",))
    lab.save("evidence/stale-after.json", {"runs": after_runs, "acquisitions": after_acq, "observations": obs, "last": outcome})
    stale_ids = {a["id"] for a in stale_acq if a["state"] == "running"}
    lab.check("killed attempt had a running acquisition", bool(stale_ids), stale_acq)
    lab.check("stale running acquisition marked abandoned (not completed)",
              all(a["state"] == "abandoned" for a in after_acq if a["id"] in stale_ids), after_acq)
    lab.check("no acquisition left running", all(a["state"] != "running" for a in after_acq), after_acq)
    lab.check("takeover eventually delivers the document", any(o["outcome"] in ("created", "changed") for o in obs), {"obs": obs, "last": outcome})
    stale_epochs = {(str(r["id"]), r["attempt_epoch"]) for r in stale}
    lab.check("no observation committed by the killed attempt epoch",
              not any((str(o["run_id"]), o["attempt_epoch"]) in stale_epochs and o["outcome"] != "failed" for o in obs), {"obs": obs, "stale": stale})
    fx.close()


# ============================================================ broken references (destructive, last)


def _snapshot_must_fail(lab, label):
    before = lab.q("SELECT count(*) AS n FROM seal.snapshot WHERE sealed")[0]["n"]
    result, receipt = lab.cli_json("snapshot", "create", label=label)
    after = lab.q("SELECT count(*) AS n FROM seal.snapshot WHERE sealed")[0]["n"]
    lab.check(f"{label}: snapshot create fails", result.returncode != 0, {"exit": result.returncode, "stdout": result.stdout[-800:]})
    lab.check(f"{label}: no new sealed snapshot", after == before, [before, after])
    return result


def _target_row(lab):
    fx = S["fx_formats"]
    return single_row(lab, "dd-017", fx.base + "/f/note.txt")


def test_corrupt_markdown_object_blocks_snapshot(lab):
    """Corrupted delivered .md object: snapshot refuses to seal; API never serves wrong bytes."""
    row = _target_row(lab)
    digest = row["markdown_hash"]
    original = lab.s3_get(digest)
    lab.s3.put_object(Bucket=lab.bucket, Key=digest, Body=original + b"\nTAMPERED")
    try:
        r = S["api"].get("/files/" + digest, "all")
        lab.check("API does not serve corrupted bytes as 200", r.status_code != 200 or sha256(r.content) == digest, [r.status_code, sha256(r.content)])
        _snapshot_must_fail(lab, "corrupt markdown")
    finally:
        lab.s3.put_object(Bucket=lab.bucket, Key=digest, Body=original)


def test_missing_original_object_blocks_snapshot(lab):
    """Missing original object: snapshot refuses to seal; API not 200."""
    row = _target_row(lab)
    digest = row["original_hash"]
    original = lab.s3_get(digest)
    lab.s3.delete_object(Bucket=lab.bucket, Key=digest)
    try:
        r = S["api"].get("/files/" + digest, "all")
        lab.check("API does not answer 200 for missing object", r.status_code != 200, r.status_code)
        _snapshot_must_fail(lab, "missing original")
    finally:
        lab.s3.put_object(Bucket=lab.bucket, Key=digest, Body=original)


def test_missing_inputs_manifest_blocks_snapshot(lab):
    """Missing inputs manifest (inputs_ref) breaks the reference closure: no sealed snapshot."""
    row = _target_row(lab)
    digest = row["inputs_ref"]
    original = lab.s3_get(digest)
    lab.s3.delete_object(Bucket=lab.bucket, Key=digest)
    try:
        _snapshot_must_fail(lab, "missing inputs manifest")
    finally:
        lab.s3.put_object(Bucket=lab.bucket, Key=digest, Body=original)


def test_snapshot_ok_after_restore(lab):
    """After restoring objects a snapshot seals again (control)."""
    result, receipt = lab.cli_json("snapshot", "create", label="snapshot after restore")
    lab.check("snapshot create succeeds after restore", result.returncode == 0 and (receipt or {}).get("sealed") is True, result.stdout[-800:])


