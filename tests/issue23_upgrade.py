"""Execute preceding Git release as a black-box fixture, then upgrade actual storage."""
from __future__ import annotations

import subprocess
import sys
import tarfile

import yaml
from issue20_fixture import html, pdf
from issue23_contract import path
from issue23_lab import ROOT, digest, write
from psycopg import sql


def catalog(lab):
    tables = lab.sql("SELECT table_name FROM information_schema.tables WHERE table_schema='seal' "
                     "AND table_type='BASE TABLE' ORDER BY table_name")
    return {r["table_name"]: [row["r"] for row in lab.sql(sql.SQL("SELECT row_to_json(t) AS r FROM {} t").format(
        sql.Identifier("seal", r["table_name"])))] for r in tables}


def preserved(before, after):
    return all(name in after and all(any(all(candidate.get(k) == v for k, v in original.items())
        for candidate in after[name]) for original in rows) for name, rows in before.items())


def upgrade(lab, data):
    legacy = lab.work / "preceding-release"
    legacy.mkdir()
    archive = lab.work / "preceding-release.tar"
    subprocess.run(["git", "archive", "--format=tar", "--output", str(archive), lab.legacy_ref,
                    "pyproject.toml", "src/seal"], cwd=ROOT, check=True, capture_output=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(legacy, filter="data")
    legacy_env = {**lab.admin_env, "PYTHONPATH": str(legacy / "src")}
    legacy_env["SEAL_DATABASE_URL"] = lab.dsn.rsplit("/", 1)[0] + "/issue23_upgrade"
    legacy_env["SEAL_WORK_DIR"] = str(lab.work / "upgrade-collector")
    lab.sql("CREATE DATABASE issue23_upgrade")
    old_cmd = [sys.executable, "-m", "seal.cli"]

    def old_cli(*args):
        result = subprocess.run([*old_cmd, *map(str, args)], cwd=legacy, env=legacy_env,
                                capture_output=True, text=True, timeout=180)
        lab.commands.append(dict(kind="legacy executable only", ref=lab.legacy_ref, argv=["seal", *map(str, args)],
                                 exit=result.returncode, stdout=lab.clean(result.stdout), stderr=lab.clean(result.stderr)))
        lab.check("preceding release CLI " + args[0] + " succeeds", result.returncode in (0, 1), exit=result.returncode)
        return result

    old_cli("init")
    lab.origin.set("/upgrade-index", html("Upgrade original", "Existing successful content must survive migration.", ("/upgrade.pdf",)))
    lab.origin.set("/upgrade.pdf", pdf("Issue23 retained legacy original bytes"), "application/pdf")
    config = dict(id="dd-008", entry_urls=[lab.origin.base + "/upgrade-index"],
                  allowed_hosts=["127.0.0.1"], allowed_path_prefixes=["/upgrade-index", "/upgrade.pdf"], archive_approved=True,
                  scope="Issue23 preceding release black-box migration input", robots=False,
                  concurrency=1, delay=0., budget=dict(requests=10, seconds=60, response_bytes=1048576),
                  crawl4ai=dict(mode="http", max_depth=1, word_count_threshold=1))
    config_path = lab.work / "dd-008.yaml"
    config_path.write_text(yaml.safe_dump(config))
    old_cli("collect", config_path, "--no-progress")
    saved = dict(dsn=lab.dsn, admin_env=lab.admin_env, env=lab.env, read_dsn=lab.read_dsn, allowed=lab.allowed)
    lab.stop_service()
    lab.dsn = legacy_env["SEAL_DATABASE_URL"]
    lab.admin_env = {**lab.admin_env, "SEAL_DATABASE_URL": lab.dsn, "SEAL_WORK_DIR": legacy_env["SEAL_WORK_DIR"]}
    lab.env = {**lab.env, "SEAL_DATABASE_URL": lab.read_dsn.rsplit("/", 1)[0] + "/issue23_upgrade"}
    lab.allowed = ["dd-008"]
    try:
        before = catalog(lab)
        objects_before = lab.object_inventory()
        lab.check("preceding release generated actual source/observation/version input", bool(before.get("source"))
                  and len(before.get("version", [])) == 2 and bool(before.get("observation")))
        migration_paths = sorted((legacy / "src/seal/migrations").glob("*.sql"))
        migration_hashes = {}
        for historical in migration_paths:
            relative = historical.relative_to(legacy)
            current = ROOT / relative
            migration_hashes[str(relative)] = digest(historical.read_bytes())
            lab.check("applied migration file remains exact " + historical.name,
                      current.exists() and digest(current.read_bytes()) == digest(historical.read_bytes()))
        lab.cli("init")
        after = catalog(lab)
        lab.check("upgrade preserves every preceding SQL row and original fields", preserved(before, after))
        lab.check("upgrade leaves existing archived S3 bytes unchanged", lab.object_inventory() == objects_before)
        lab.check("upgrade creates no snapshots and leaves publication pointer empty", lab.pointer() is None
                  and lab.sql("SELECT count(*) AS n FROM seal.snapshot")[0]["n"] == 0)
        lab.sql("GRANT CONNECT ON DATABASE issue23_upgrade TO api_reader")
        lab.sql("GRANT USAGE ON SCHEMA seal TO api_reader")
        lab.sql("GRANT SELECT ON ALL TABLES IN SCHEMA seal TO api_reader")
        lab.start_service()
        for target in ("/sources", path(config["entry_urls"][0]), "/attachments/made-up"):
            lab.error(target, "snapshot_not_published", 503)
        created = lab.json_cli("snapshot", "create")
        ident = created.get("snapshot_id", created.get("id"))
        inspected = lab.json_cli("snapshot", "inspect", ident)
        lab.check("post-upgrade explicit create and inspect expose retained two successful resources", len(inspected.get("items", [])) == 2
                  and lab.pointer() is None)
        lab.publish(ident)
        status, _, values, _, _ = lab.request(path(config["entry_urls"][0]))
        lab.check("post-upgrade explicit publish serves retained content", status == 200 and isinstance(values, list) and len(values) == 2)
        if isinstance(values, list):
            for value in values:
                for attachment in value["attachments"]:
                    status, _, _, raw, _ = lab.request(attachment["url"])
                    lab.check("post-upgrade original PDF attachment bytes retained", status == 200 and raw == lab.origin.original_files[digest(raw)])
        write(lab.artifacts / "upgrade.json", dict(legacy_ref=lab.legacy_ref,
              old_executable_only=True, previous_tables=before, after_tables=after,
              applied_migration_hashes=migration_hashes, mapping=lab.mapping(ident),
              publication=lab.audit(), object_inventory=objects_before))
    finally:
        lab.stop_service()
        for name, value in saved.items():
            setattr(lab, name, value)
