"""Explicit schema upgrades with a catalog inventory and a verified backup."""

import os
import subprocess
from pathlib import Path

from psycopg import pq, sql
from psycopg.conninfo import conninfo_to_dict

from .core import SealError, digest
from .db import connect, dsn
from .objects import Objects

CURRENT = "0013_global_snapshots.sql"
ARCHIVE_CURRENT = "0012_minimal_archive.sql"
DELETED = {
    "seal_record",
    "seal_record_result",
    "seal_record_version",
    "seal_record_emission",
    "seal_result",
}


def require_current():
    with connect() as c:
        if c.execute("SELECT to_regclass('seal.migration') AS name").fetchone()["name"] is None:
            raise SealError("database_init_required")
        if not c.execute(
            "SELECT 1 FROM seal.migration WHERE name=%s AND checksum=%s",
            (CURRENT, digest((Path(__file__).parent / "migrations" / CURRENT).read_bytes())),
        ).fetchone():
            raise SealError("database_upgrade_required")


def inventory(c):
    objects = c.execute("""SELECT n.nspname AS schema,c.relname AS name,c.relkind AS kind,
        pg_catalog.pg_get_userbyid(c.relowner) AS owner, parent.relname AS table_name
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_catalog.pg_index i ON i.indexrelid=c.oid
        LEFT JOIN pg_catalog.pg_class parent ON parent.oid=i.indrelid
        WHERE (n.nspname='public' AND (c.relname LIKE 'seal\\_%' OR c.relname LIKE 'procrastinate\\_%'))
           OR n.nspname='seal' ORDER BY n.nspname,c.relname""").fetchall()
    for obj in objects:
        obj["action"] = (
            "delete"
            if obj["name"] in DELETED or obj["table_name"] in DELETED
            else "third_party"
            if obj["name"].startswith("procrastinate_")
            else "retain"
        )
        if obj["kind"] in ("r", "p", "v", "m"):
            obj["rows"] = c.execute(
                sql.SQL("SELECT count(*) AS n FROM {}.{}").format(
                    sql.Identifier(obj["schema"]), sql.Identifier(obj["name"])
                )
            ).fetchone()["n"]
        obj["target"] = (
            None
            if obj["action"] == "delete"
            else (
                obj["schema"] + "." + obj["name"]
                if obj["action"] == "third_party" or obj["schema"] == "seal"
                else "seal." + obj["name"].removeprefix("seal_")
            )
        )
    constraints = c.execute("""SELECT n.nspname AS schema,t.relname AS table_name,c.conname AS name,
        c.contype AS kind,pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c
        JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname='seal' OR (n.nspname='public' AND (t.relname LIKE 'seal\\_%' OR t.relname LIKE 'procrastinate\\_%'))
        ORDER BY n.nspname,t.relname,c.conname""").fetchall()
    triggers = c.execute("""SELECT n.nspname AS schema,c.relname AS table_name,t.tgname AS name,
        pg_get_triggerdef(t.oid) AS definition FROM pg_trigger t
        JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE NOT t.tgisinternal AND (n.nspname='seal' OR (n.nspname='public' AND (c.relname LIKE 'seal\\_%' OR c.relname LIKE 'procrastinate\\_%')))
        ORDER BY n.nspname,c.relname,t.tgname""").fetchall()
    functions = c.execute("""SELECT n.nspname AS schema,p.proname AS name,pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='seal' OR (n.nspname='public' AND (p.proname LIKE 'seal\\_%' OR p.proname LIKE 'procrastinate\\_%'))
        ORDER BY n.nspname,p.proname""").fetchall()
    dependencies = c.execute("""SELECT pg_describe_object(d.classid,d.objid,d.objsubid) AS dependent,
        pg_describe_object(d.refclassid,d.refobjid,d.refobjsubid) AS referenced,d.deptype AS type
        FROM pg_depend d WHERE d.refclassid='pg_class'::regclass AND d.refobjid IN (
         SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname='seal' OR (n.nspname='public' AND c.relname LIKE 'seal\\_%'))
        ORDER BY dependent,referenced""").fetchall()
    target = c.execute(
        "SELECT current_database() AS database,inet_server_addr()::text AS address,inet_server_port() AS port"
    ).fetchone()
    for obj in [*constraints, *triggers, *functions]:
        third_party = obj.get("table_name", obj["name"]).startswith("procrastinate_")
        deleted = obj.get("table_name") in DELETED or obj["name"] == "seal_result_immutable"
        obj["action"] = "third_party" if third_party else "delete" if deleted else "retain"
        obj["target"] = (
            None
            if deleted
            else (
                obj["schema"] + "." + obj["name"]
                if third_party or obj["schema"] == "seal"
                else "seal." + obj["name"].removeprefix("seal_")
            )
        )
    return {
        "target": target,
        "objects": objects,
        "constraints": constraints,
        "triggers": triggers,
        "functions": functions,
        "dependencies": dependencies,
    }


def backup_database(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Refuse overwrite; a prior backup is a valuable rollback artifact.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        values = conninfo_to_dict(dsn())
        # Libpq environment defaults do not expand a URI in PGDATABASE.
        # Map through libpq's documented environment names; keep credentials
        # out of command arguments and diagnostic output.
        names = {
            o.keyword.decode(): o.envvar.decode() for o in pq.Conninfo.get_defaults() if o.envvar
        }
        env = dict(os.environ)
        env.update({names[key]: value for key, value in values.items() if key in names})
        result = subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(path)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )
        if result.returncode or not path.stat().st_size:
            raise SealError("database_backup_failed")
        verified = subprocess.run(
            ["pg_restore", "--list", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if verified.returncode:
            raise SealError("database_backup_invalid")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"path": str(path), "sha256": digest(path.read_bytes()), "bytes": path.stat().st_size}


def initialize(*, backup=None, acknowledge=False):
    Objects().check()
    with connect() as c:
        before = inventory(c)
        old = (
            c.execute("SELECT to_regclass('public.seal_source') AS name").fetchone()["name"]
            is not None
        )
        timeline_upgrade = (
            not old
            and c.execute("SELECT to_regclass('seal.source') AS name").fetchone()["name"]
            is not None
            and not c.execute(
                "SELECT 1 FROM seal.migration WHERE name=%s", (ARCHIVE_CURRENT,)
            ).fetchone()
        )
        if old or timeline_upgrade:
            content_tables = {
                "observation",
                "fetch_observation",
                "version",
                "snapshot",
                "revision",
                "document",
                "document_revision",
                "document_version",
                "document_emission",
                "record",
                "record_version",
                "record_result",
                "record_emission",
                "result",
            }
            if any(
                obj.get("rows", 0) and obj["name"].removeprefix("seal_") in content_tables
                for obj in before["objects"]
            ):
                raise SealError("archive_contract_upgrade_requires_empty_content")
        if timeline_upgrade and not backup:
            return {
                "status": "upgrade_confirmation_required",
                "inventory": before,
                "error": "backup_required_for_timeline_upgrade",
            }
        if old and not (backup and acknowledge):
            return {
                "status": "upgrade_confirmation_required",
                "inventory": before,
                "error": "backup_and_data_loss_acknowledgement_required",
            }
        active = c.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'"
        ).fetchone()["n"]
        if (old or timeline_upgrade) and active:
            raise SealError("stop_database_clients_before_upgrade")
    saved = backup_database(backup) if (old or timeline_upgrade) else None
    from .queue import app

    with connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(72032101)")
        if old or timeline_upgrade:
            # Recheck under table locks after the backup. A writer racing the
            # initial inventory must not sneak historical content into this upgrade.
            for obj in before["objects"]:
                if (
                    obj["kind"] in ("r", "p")
                    and obj["name"].removeprefix("seal_") in content_tables
                ):
                    table = sql.Identifier(obj["schema"], obj["name"])
                    c.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(table))
                    if c.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(table)).fetchone():
                        raise SealError("archive_contract_upgrade_requires_empty_content")
        ledger = (
            "seal.migration"
            if c.execute("SELECT to_regclass('seal.migration') AS name").fetchone()["name"]
            else "public.seal_migration"
        )
        if ledger == "public.seal_migration":
            c.execute(
                "CREATE TABLE IF NOT EXISTS public.seal_migration(name text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())"
            )
        root = Path(__file__).parent / "migrations"
        applied = []
        for path in [root / "schema.sql", *sorted(root.glob("[0-9]*.sql"))]:
            checksum = digest(path.read_bytes())
            previous = c.execute(
                sql.SQL("SELECT checksum FROM {} WHERE name=%s").format(
                    sql.Identifier(*ledger.split("."))
                ),
                (path.name,),
            ).fetchone()
            if previous:
                if previous["checksum"] != checksum:
                    raise SealError("applied_migration_changed")
                continue
            if path.name == "0011_result_references.sql":
                # Historical SQL remains immutable in the ledger. Only empty content
                # is supported; this is schema bootstrap, never a result backfill.
                c.execute(
                    "CREATE TEMP TABLE result_refs (observation_id text PRIMARY KEY, ref text NOT NULL) ON COMMIT DROP"
                )
            c.execute(path.read_text())
            if path.name == "0009_document_only.sql":
                ledger = "seal.migration"
            c.execute(
                sql.SQL("INSERT INTO {}(name,checksum) VALUES(%s,%s)").format(
                    sql.Identifier(*ledger.split("."))
                ),
                (path.name, checksum),
            )
            applied.append(path.name)
        installed = c.execute("SELECT to_regclass('procrastinate_jobs') AS name").fetchone()["name"]
        leftovers = c.execute("""SELECT name FROM (
            SELECT c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname LIKE 'seal\\_%'
            UNION ALL SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' AND p.proname LIKE 'seal\\_%') old_objects""").fetchall()
        if leftovers:
            raise SealError("unmigrated_database_objects")
    if installed is None:
        with app.open():
            app.schema_manager.apply_schema()
    with connect() as c:
        after = inventory(c)
        queue = c.execute("""SELECT n.nspname || '.' || r.relname AS name
          FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace
          WHERE r.oid=to_regclass('procrastinate_jobs')""").fetchone()["name"]
    return {
        "status": "initialized",
        "schema": "resource-timeline",
        "applied": applied,
        "backup": saved,
        "before": before,
        "after": after,
        "queue": queue,
    }
