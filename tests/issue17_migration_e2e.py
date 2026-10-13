"""历史迁移黑盒：执行冻结旧版本 CLI，升级并实际恢复 requested backup。

不阅读旧/新实现源码；旧树只作为独立可执行 fixture。每张旧表只比较其原有列，
允许 additive migration 增加列和行，但不得删除或重写任何旧业务行/SQL对象。
"""

from __future__ import annotations

import hashlib
import subprocess
import tarfile
import traceback
from pathlib import Path

import yaml
from issue17_fixture import Fixture
from issue17_support import Lab, json_write

LEGACY_REF = "04a50e59170dd592b4756ae9c048bd21a87f79b6"


def catalog(lab: Lab):
    tables = lab.sql("SELECT table_name FROM information_schema.tables WHERE table_schema='seal' AND table_type='BASE TABLE' ORDER BY table_name")
    return {row["table_name"]: lab.sql(f'SELECT row_to_json(t) AS row FROM seal."{row["table_name"]}" t') for row in tables}


def rows_unchanged(before, after):
    for table, records in before.items():
        if table not in after:
            return False
        newer = [entry["row"] for entry in after[table]]
        for entry in records:
            old = entry["row"]
            if not any(all(candidate.get(key) == value for key, value in old.items()) for candidate in newer):
                return False
    return True


def run_migration(lab: Lab, fixture, legacy_ref=LEGACY_REF):
    legacy = lab.work / "legacy-checkout"
    legacy.mkdir()
    archive = lab.work / "legacy-checkout.tar"
    subprocess.run(["git", "archive", "--format=tar", "--output", str(archive), legacy_ref,
                    "pyproject.toml", "uv.lock", "src/seal", "recipes", "examples"],
                   cwd=lab.root, capture_output=True, check=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(legacy, filter="data")
    baseline_commit = subprocess.check_output(["git", "rev-parse", "--verify", legacy_ref + "^{commit}"], cwd=lab.root, text=True).strip()
    metadata = {"requested_ref": legacy_ref, "commit": baseline_commit,
                "checkout": str(legacy), "only_executed_not_read": True}
    json_write(lab.artifacts / "legacy-fixture.json", metadata)
    installed = subprocess.run(["uv", "sync", "--frozen", "--project", str(legacy)],
                               cwd=lab.root, env=lab.env, capture_output=True, text=True, timeout=300)
    (lab.artifacts / "legacy-dependencies.stderr.txt").write_text(installed.stderr)
    lab.check("legacy fixture isolated dependency environment", installed.returncode == 0)
    if installed.returncode:
        raise RuntimeError("Could not create isolated historical executable fixture")
    old_database = lab.database_url
    old_archive = lab.archive
    old_environment = lab.env.copy()
    legacy_url = old_database.rsplit("/", 1)[0] + "/seal_legacy"
    restore_url = old_database.rsplit("/", 1)[0] + "/seal_backup_restored"
    lab.sql("CREATE DATABASE seal_legacy")
    lab.sql("CREATE DATABASE seal_backup_restored")
    lab.database_url = legacy_url
    lab.archive = lab.work / "legacy-archive"
    lab.archive.mkdir()
    lab.env.update({"SEAL_DATABASE_URL": legacy_url, "SEAL_ARCHIVE": str(lab.archive)})
    try:
        old_executable = [str(legacy / ".venv" / "bin" / "seal")]
        old_env = lab.env.copy()
        old_env.pop("PYTHONPATH", None)
        lab.command("init", executable=old_executable, env=old_env)
        config = {
            "id": "dd-001", "recipe": {"path": str(legacy / "recipes" / "documents"), "params": {}},
            "entry_urls": [fixture.base + "/b/"], "allowed_hosts": ["127.0.0.1"],
            "allowed_path_prefixes": ["/"], "archive_approved": True,
            "scope": "Historical synthetic migration fixture", "robots": True,
            "budget": {"requests": 30, "seconds": 60, "response_bytes": 1048576},
        }
        configuration = lab.work / "legacy-fixture"
        configuration.mkdir()
        path = configuration / "dd-001.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False))
        lab.command("collect", str(path), "--no-progress", executable=old_executable, env=old_env)
        before = catalog(lab)
        old_hashes = lab.archive_hashes()
        json_write(lab.artifacts / "legacy-before.database.json", before)
        json_write(lab.artifacts / "legacy-before.archive-hashes.json", old_hashes)
        lab.check("legacy CLI created real historical content", bool(before.get("document_version")) and bool(old_hashes))
        backup = lab.artifacts / "legacy-before-upgrade.dump"
        lab.command("init", "--backup", str(backup))
        after = catalog(lab)
        json_write(lab.artifacts / "legacy-after.database.json", after)
        lab.check("migration preserves old SQL tables and all original row fields", rows_unchanged(before, after))
        lab.check("migration leaves historical archive exact bytes", all(lab.archive_hashes().get(key) == value for key, value in old_hashes.items()))
        lab.check("requested pre-migration backup persisted", backup.exists() and backup.stat().st_size > 0)
        if backup.exists():
            restored = subprocess.run(["pg_restore", "--exit-on-error", "--dbname", restore_url, str(backup)], capture_output=True, text=True)
            (lab.artifacts / "backup-restore.stderr.txt").write_text(restored.stderr)
            lab.check("actual pg_restore backup succeeds", restored.returncode == 0, exit_code=restored.returncode)
            lab.database_url = restore_url
            restore_catalog = catalog(lab)
            json_write(lab.artifacts / "legacy-restored.database.json", restore_catalog)
            lab.check("restored backup reproduces all pre-upgrade original rows", rows_unchanged(before, restore_catalog))
        legacy_files = {}
        for historical in (legacy / "src" / "seal" / "migrations").glob("*.sql"):
            relative = historical.relative_to(legacy)
            current = lab.root / relative
            digest = hashlib.sha256(historical.read_bytes()).hexdigest()
            legacy_files[str(relative)] = digest
            lab.check("historical migration remains exact " + historical.name,
                      current.exists() and hashlib.sha256(current.read_bytes()).hexdigest() == digest)
        json_write(lab.artifacts / "historical-migration-hashes.json", legacy_files)
    finally:
        lab.database_url = old_database
        lab.archive = old_archive
        lab.env = old_environment


if __name__ == "__main__":
    lab = Lab(Path(__file__).resolve().parents[1])
    fixture = Fixture(lab.work / "fixture")
    try:
        lab.start_database()
        fixture.start()
        run_migration(lab, fixture)
    except Exception as error:
        lab.check("historical migration oracle", False, error=str(error), traceback=traceback.format_exc())
    finally:
        if fixture.thread.is_alive():
            fixture.close()
        lab.close()
        report = lab.report()
        print({key: report[key] for key in ("run", "passed", "failed", "artifacts")}, flush=True)
    raise SystemExit(1 if report["failed"] else 0)
