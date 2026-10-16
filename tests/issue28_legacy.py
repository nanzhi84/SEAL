"""Reproduce real ba9db3a legacy data, upgrade, and conflicting creator history.

Run standalone: uv run --no-sync python tests/issue28_legacy.py
Requires local pg_dump and pg_restore compatible with PostgreSQL 17, in addition
to issue28_runtime prerequisites. The CLI itself owns creation of upgrade dumps.
"""

import hashlib
import io
import json
import secrets
import subprocess
import tarfile
import time
import urllib.request
import uuid

from cryptography.fernet import Fernet
from issue28_runtime import ROOT, Runtime, free_port

BASELINE = "ba9db3a2c006d26b16d04f1262508a4045500d07"


def legacy_deployment(rt):
    destination = rt.work / "baseline"
    destination.mkdir()
    archive = subprocess.check_output(["git", "archive", BASELINE, "src"], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(destination, filter="data")
    return dict(rt.env, PYTHONPATH=str(destination / "src"))


def seed(rt):
    env = legacy_deployment(rt)
    rt.cli("init", env=env)
    rt.fixture.html("/legacy", "LEGACY PRESERVED CONTENT")
    config = rt.config("dd-017", ["/legacy"])
    rt.collect(config, env=env)
    _, captured = rt.cli("snapshot", "create", env=env)
    sid = captured["snapshot_id"]
    rt.cli("snapshot", "publish", sid, env=env)
    return config, sid, rt.state()


def consume_legacy_snapshot(rt, sid):
    token, port = secrets.token_urlsafe(30), free_port()
    path = rt.work / "api.local.json"
    base = f"http://127.0.0.1:{port}"
    path.write_text(
        json.dumps(
            {
                "public_base_url": base,
                "token_key": Fernet.generate_key().decode(),
                "callers": [
                    {
                        "id": "legacy",
                        "key_sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "sources": ["dd-017"],
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    rt.protect(token, json.loads(path.read_text())["token_key"])
    proc = rt.spawn("serve", "--config", path, "--host", "127.0.0.1", "--port", str(port))
    request = urllib.request.Request(
        base + "/results?source_url=" + rt.fixture.base + "/legacy",
        headers={"Authorization": "Bearer " + token},
    )
    for _ in range(80):
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
            break
        except OSError:
            if proc.poll() is not None:
                raise AssertionError("upgraded legacy API exited") from None
            time.sleep(0.25)
    rt.check(
        "existing sealed legacy snapshot remains consumable through API",
        len(result) == 1 and "LEGACY PRESERVED CONTENT" in result[0]["markdown"],
    )
    rt.save("legacy-consumption.json", {"snapshot_id": sid, "results": result})


def upgrade_success():
    with Runtime("issue28-legacy") as rt:
        config, sid, before = seed(rt)
        old_version = before["version"][0]
        rt.cli("init", "--backup", rt.work / "before-upgrade.dump")
        after = rt.state()
        for table in ("snapshot", "snapshot_source", "snapshot_item", "publication"):
            rt.check("migration preserves existing " + table, before[table] == after[table])
        migrated = rt.versions("dd-017")[0]
        rt.check(
            "migration preserves historical version origin and object facts",
            all(
                old_version[key] == migrated[key]
                for key in (
                    "id",
                    "observation_id",
                    "resource_id",
                    "body_hash",
                    "result_ref",
                    "sequence",
                    "cursor",
                )
            ),
        )
        legacy_fp = migrated.get("parser_fingerprint")
        rt.check(
            "unknown historical parser is recognizably legacy or nullable",
            legacy_fp is None or any(x in str(legacy_fp).lower() for x in ("legacy", "unknown")),
        )
        rt.cli("snapshot", "inspect", sid, "--limit", "1")
        rt.collect(config)
        first_count = len(rt.versions("dd-017"))
        rt.check(
            "first post-upgrade observation has at most one transition version",
            first_count in (1, 2),
        )
        rt.collect(config)
        rt.check(
            "stable post-upgrade observation never repeats transition version",
            len(rt.versions("dd-017")) == first_count,
        )
        _, newer = rt.cli("snapshot", "create")
        rt.cli("snapshot", "publish", newer["snapshot_id"])
        rt.cli("snapshot", "publish", sid)
        consume_legacy_snapshot(rt, sid)
        rt.save(
            "legacy-upgrade.json",
            {
                "baseline": BASELINE,
                "before": before,
                "after_migration": after,
                "versions_after_two_observations": rt.versions("dd-017"),
                "observations_after_two": rt.observations("dd-017"),
            },
        )
        return str(rt.artifacts)


def upgrade_conflict():
    with Runtime("issue28-legacy-conflict") as rt:
        _, sid, state = seed(rt)
        duplicate = dict(state["version"][0])
        duplicate.update(id=str(uuid.uuid4()), sequence=2, cursor=duplicate["cursor"] + 1)
        with rt.db() as conn:
            conn.execute(
                "INSERT INTO seal.version SELECT * FROM json_populate_record(NULL::seal.version,%s::json)",
                (json.dumps(duplicate, default=str),),
            )
        before = rt.state()
        rt.check(
            "legacy conflict fixture represents two versions created by one observation",
            len(before["version"]) == 2
            and len({x["observation_id"] for x in before["version"]}) == 1,
        )
        code, error = rt.cli("init", "--backup", rt.work / "before-conflict.dump", success=False)
        after = rt.state()
        rt.check(
            "migration refuses creator conflict and reports diagnostic",
            code != 0
            and "observation" in json.dumps(error).lower()
            and any(x in json.dumps(error).lower() for x in ("conflict", "duplicate")),
        )
        rt.check("failed migration preserves every prior fact and snapshot", before == after)
        columns = rt.query(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='seal' AND table_name='version'"
        )
        rt.check(
            "failed additive migration leaves old schema intact",
            "parser_fingerprint" not in {x["column_name"] for x in columns},
        )
        rt.save(
            "legacy-conflict.json",
            {
                "baseline": BASELINE,
                "snapshot_id": sid,
                "cli_error": error,
                "before": before,
                "after": after,
            },
        )
        return str(rt.artifacts)


def run_legacy():
    return [upgrade_success(), upgrade_conflict()]


if __name__ == "__main__":
    run_legacy()
