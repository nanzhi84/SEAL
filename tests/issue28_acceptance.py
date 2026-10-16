"""Issue #28 public CLI → isolated storage → published read API acceptance.

Run: uv run --no-sync python tests/issue28_acceptance.py
Preparation: uv sync --frozen; uv run --no-sync patchright install chromium;
docker pull postgres:17-alpine. Build the compose minio service if its named
image is absent. pg_dump/pg_restore 17 must be on PATH for legacy upgrades.
All IDs and raw inputs are synthetic. Failures retain sanitized evidence under
artifacts/issue28-*/; secrets and subprocess working files stay under work/.
The test deliberately does not import SEAL implementation modules.
"""

import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from issue28_inputs import adjust, fingerprint_matrix, input_modes
from issue28_runtime import Runtime, free_port


def snapshot(rt, overrides=None, success=True):
    args = ["snapshot", "create"]
    if overrides is not None:
        path = rt.work / f"overrides-{len(rt.commands)}.json"
        path.write_text(json.dumps(overrides))
        args += ["--version-overrides", path]
    return rt.cli(*args, success=success)


def items(rt, sid):
    return rt.query(
        "SELECT * FROM seal.snapshot_item WHERE snapshot_id=%s ORDER BY resource_id", (sid,)
    )


def snapshots(rt, matrix, history, old_pdf):
    _, default = snapshot(rt)
    default_id = default["snapshot_id"]
    default_items = items(rt, default_id)
    expected = rt.query(
        "SELECT id,current_version_id FROM seal.resource WHERE current_version_id IS NOT NULL"
    )
    rt.check(
        "default snapshot freezes exact latest successful resources",
        {x["resource_id"]: x["version_id"] for x in default_items}
        == {x["id"]: x["current_version_id"] for x in expected},
    )
    stale = rt.versions("dd-030")[-1]
    rt.check(
        "snapshot retains last success despite latest failed observation",
        any(x["version_id"] == stale["id"] for x in default_items)
        and rt.observations("dd-030")[-1]["version_id"] is None,
    )
    rt.cli("snapshot", "publish", default_id)
    before = rt.state()
    overrides = {
        "overrides": [
            {"resource_id": history[1]["resource_id"], "version_id": history[1]["id"]},
            {"resource_id": old_pdf["resource_id"], "version_id": old_pdf["id"]},
        ]
    }
    _, picked = snapshot(rt, overrides)
    picked_id = picked["snapshot_id"]
    selected = items(rt, picked_id)
    mapping = {x["resource_id"]: x["version_id"] for x in default_items}
    mapping.update({x["resource_id"]: x["version_id"] for x in overrides["overrides"]})
    rt.check(
        "overrides affect only requested resources",
        {x["resource_id"]: x["version_id"] for x in selected} == mapping,
    )
    after = rt.state()
    rt.check(
        "create override preserves current pointers and observation/version history",
        all(
            before[key] == after[key]
            for key in ("resource", "observation", "version", "publication")
        ),
    )
    rt.check("previous frozen snapshot is unchanged", items(rt, default_id) == default_items)
    rejected = False
    try:
        with rt.db() as conn:
            conn.execute(
                "UPDATE seal.snapshot_item SET version_id=version_id WHERE snapshot_id=%s",
                (picked_id,),
            )
    except psycopg.DatabaseError:
        rejected = True
    rt.check(
        "frozen mapping update guard remains enforced",
        rejected and items(rt, picked_id) == selected,
    )
    rt.check(
        "global snapshot includes multiple sources", len({x["source_id"] for x in selected}) >= 5
    )
    for sid in (default_id, picked_id):
        row = rt.query("SELECT * FROM seal.snapshot WHERE id=%s", (sid,))[0]
        sources = rt.query("SELECT * FROM seal.snapshot_source WHERE snapshot_id=%s", (sid,))
        members = items(rt, sid)
        rt.check(
            "snapshot sealed counts and source ownership " + sid,
            row["sealed"]
            and row["source_count"] == len(sources)
            and row["resource_count"] == len(members)
            and row["version_count"] == len({x["version_id"] for x in members})
            and {x["source_id"] for x in members} <= {x["source_id"] for x in sources},
        )
        rt.cli("snapshot", "inspect", sid, "--limit", "1")
    rt.cli("snapshot", "list")
    invalids = [
        None,
        {},
        {"overrides": "wrong"},
        {"overrides": [{}]},
        {"overrides": [overrides["overrides"][0]] * 2},
        {"overrides": [{"resource_id": str(uuid.uuid4()), "version_id": history[0]["id"]}]},
        {
            "overrides": [
                {"resource_id": history[0]["resource_id"], "version_id": str(uuid.uuid4())}
            ]
        },
        {"overrides": [{"resource_id": history[0]["resource_id"], "version_id": matrix[0]["id"]}]},
    ]
    initial = rt.query("SELECT * FROM seal.snapshot ORDER BY id")
    for index, invalid in enumerate(invalids):
        if invalid is None:
            path = rt.work / "malformed.json"
            path.write_text("{unclosed")
            code, _ = rt.cli("snapshot", "create", "--version-overrides", path, success=False)
        else:
            code, _ = snapshot(rt, invalid, success=False)
        rt.check(
            "invalid override fails atomically " + str(index),
            code != 0 and rt.query("SELECT * FROM seal.snapshot ORDER BY id") == initial,
        )
    key = old_pdf["result_ref"]
    raw = rt.s3.get_object(Bucket="issue28", Key=key)["Body"].read()
    rt.s3.delete_object(Bucket="issue28", Key=key)
    code, _ = snapshot(rt, overrides, success=False)
    rt.check(
        "unreadable selected result rejects entire snapshot",
        code != 0 and rt.query("SELECT * FROM seal.snapshot ORDER BY id") == initial,
    )
    publication = rt.query("SELECT * FROM seal.publication")
    code, _ = rt.cli("snapshot", "publish", picked_id, success=False)
    rt.check(
        "unreadable publication preserves previous published pointer",
        code != 0 and rt.query("SELECT * FROM seal.publication") == publication,
    )
    rt.s3.put_object(Bucket="issue28", Key=key, Body=raw)
    attachment_key = rt.archive(old_pdf)["attachment_refs"][0]
    attachment_raw = rt.s3.get_object(Bucket="issue28", Key=attachment_key)["Body"].read()
    rt.s3.delete_object(Bucket="issue28", Key=attachment_key)
    code, _ = snapshot(rt, overrides, success=False)
    rt.check(
        "unreadable selected attachment rejects entire snapshot",
        code != 0 and rt.query("SELECT * FROM seal.snapshot ORDER BY id") == initial,
    )
    rt.s3.put_object(Bucket="issue28", Key=attachment_key, Body=attachment_raw)
    rt.fixture.html("/updated-entry", "NEW SOURCE ENTRY")
    source_config = rt.work / "dd-040.yaml"
    adjust(source_config, entry_urls=[rt.fixture.base + "/updated-entry"], parser={"mode": "http"})
    rt.collect(source_config)
    rt.check(
        "new collection cannot mutate prior snapshot membership",
        items(rt, default_id) == default_items and items(rt, picked_id) == selected,
    )
    rt.save(
        "snapshots.json",
        {
            "default": default,
            "picked": picked,
            "default_items": default_items,
            "picked_items": selected,
            "overrides": overrides,
        },
    )
    return default_id, picked_id, default_items, selected


def consume(rt, snapshots_data, history, old_pdf):
    default_id, picked_id, default_items, selected = snapshots_data
    token, port = secrets.token_urlsafe(30), free_port()
    base = f"http://127.0.0.1:{port}"
    config = {
        "public_base_url": base,
        "token_key": Fernet.generate_key().decode(),
        "callers": [
            {
                "id": "issue28",
                "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "sources": sorted({x["source_id"] for x in default_items}),
            }
        ],
    }
    rt.protect(token, config["token_key"])
    path = rt.work / "api.local.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    proc = rt.spawn("serve", "--config", path, "--host", "127.0.0.1", "--port", str(port))

    def request(url):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            return response.status, response.headers, raw

    for _ in range(60):
        try:
            _, _, raw = request(base + "/sources")
            break
        except (OSError, urllib.error.HTTPError):
            if proc.poll() is not None:
                raise AssertionError("API exited") from None
            time.sleep(0.25)
    rt.check(
        "consumer discovers frozen authorized sources",
        {x["id"] for x in json.loads(raw)} == {x["source_id"] for x in default_items},
    )
    source = next(x for x in json.loads(raw) if x["id"] == "dd-040")
    rt.check(
        "consumer discovers original frozen source entries after live config changes",
        rt.fixture.base + "/history" in source["entry_urls"]
        and rt.fixture.base + "/updated-entry" not in source["entry_urls"],
    )
    rt.cli("snapshot", "publish", picked_id)
    url = (
        base
        + "/results?"
        + urllib.parse.urlencode({"source_url": rt.fixture.base + "/history", "limit": 1})
    )
    _, headers, raw = request(url)
    first_page = json.loads(raw)
    rt.check(
        "real consumption has bounded first page and next cursor",
        len(first_page) == 1 and bool(headers.get("Link")),
    )
    next_url = re.search(r"<([^>]+)>", headers["Link"])[1]
    rt.cli("snapshot", "publish", default_id)
    _, _, raw = request(next_url)
    second_page = json.loads(raw)
    pages = first_page + second_page
    rt.check(
        "continuation remains on frozen old snapshot after publication changes",
        len(pages) == 2
        and len({x["url"] for x in pages}) == 2
        and any("HISTORY BRAVO" in x["markdown"] for x in pages),
    )
    rt.cli("snapshot", "publish", picked_id)
    _, _, raw = request(
        base
        + "/results?"
        + urllib.parse.urlencode({"source_url": rt.fixture.base + "/document.pdf"})
    )
    pdf_results = json.loads(raw)
    rt.check(
        "overridden historical PDF content reaches public API",
        len(pdf_results) == 1
        and "PDF ALPHA" in pdf_results[0]["markdown"]
        and bool(pdf_results[0]["attachments"]),
    )
    _, _, attachment = request(pdf_results[0]["attachments"][0]["url"])
    rt.check(
        "public attachment is selected historical raw bytes",
        hashlib.sha256(attachment).hexdigest() == old_pdf["body_hash"],
    )
    before = rt.query("SELECT count(*) AS n FROM seal.snapshot_publication")[0]["n"]
    rt.cli("snapshot", "publish", picked_id)
    rt.check(
        "explicit repeat publication is idempotent",
        before == rt.query("SELECT count(*) AS n FROM seal.snapshot_publication")[0]["n"],
    )
    rt.save(
        "consumption.json",
        {
            "first_page": first_page,
            "second_page_after_switch": second_page,
            "historical_pdf": pdf_results,
            "attachment_sha256": hashlib.sha256(attachment).hexdigest(),
        },
    )
    rt.check(
        "consumption did not mutate frozen mappings",
        items(rt, default_id) == default_items and items(rt, picked_id) == selected,
    )


def main():
    with Runtime() as rt:
        rt.cli("init")
        matrix = fingerprint_matrix(rt)
        history, old_pdf, _ = input_modes(rt)
        from issue28_resilience import (
            deployment_contracts,
            representation_inputs,
            storage_atomicity,
        )

        deployment_contracts(rt)
        representation_inputs(rt)
        storage_atomicity(rt)
        snapshot_data = snapshots(rt, matrix, history, old_pdf)
        consume(rt, snapshot_data, history, old_pdf)
        from issue28_resilience import concurrency_and_retry

        concurrency_and_retry(rt)
        rt.save("final-state.json", rt.state())
    from issue28_legacy import run_legacy

    legacy_paths = run_legacy()
    components = [rt.artifacts, *map(Path, legacy_paths)]
    results = [
        {"artifacts": str(path), **json.loads((path / "result.json").read_text())}
        for path in components
    ]
    test_digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("issue28*.py")):
        test_digest.update(path.name.encode() + b"\0" + path.read_bytes())
    rt.save(
        "suite.json",
        {
            "pass": all(row["pass"] for row in results),
            "components": results,
            "assertions": sum(row["assertions"] for row in results),
            "tests_sha256": test_digest.hexdigest(),
            "command": "uv run --no-sync python tests/issue28_acceptance.py",
        },
    )
    from issue28_artifact_audit import audit

    rt.save("sensitive-evidence-audit.json", audit())


if __name__ == "__main__":
    main()
