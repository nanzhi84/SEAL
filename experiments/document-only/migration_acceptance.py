"""Upgrade a populated old schema, independently count data and restore its backup."""

import hashlib
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

TABLES = [
    "source",
    "recipe_version",
    "binding",
    "run",
    "fetch_observation",
    "document",
    "revision",
    "document_version",
    "document_emission",
    "document_reference",
    "discovery",
    "decision",
]


def exercise(h):
    original = h.env["SEAL_DATABASE_URL"]
    with psycopg.connect(original, autocommit=True) as c:
        c.execute("CREATE DATABASE legacy")
    old_dsn = original.rsplit("/", 1)[0] + "/legacy"
    with psycopg.connect(old_dsn, row_factory=dict_row) as c:
        c.execute(
            "CREATE TABLE public.seal_migration(name text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())"
        )
        root = Path("src/seal")
        for path in [root / "schema.sql", *sorted((root / "migrations").glob("000[2-8]*.sql"))]:
            c.execute(path.read_text())
            c.execute(
                "INSERT INTO public.seal_migration(name,checksum) VALUES(%s,%s)",
                (path.name, hashlib.sha256(path.read_bytes()).hexdigest()),
            )
        # Copy real CLI-produced document evidence into old-schema columns.
        for table in TABLES:
            columns = [
                r["column_name"]
                for r in c.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                    ("seal_" + table,),
                )
            ]
            rows = (
                h.sql(
                    sql.SQL("SELECT * FROM seal.{} ORDER BY created_at").format(
                        sql.Identifier(table)
                    )
                )
                if table
                in {
                    "revision",
                    "run",
                    "document_version",
                    "document_emission",
                    "discovery",
                    "decision",
                    "binding",
                    "source",
                    "recipe_version",
                }
                else h.sql(sql.SQL("SELECT * FROM seal.{}").format(sql.Identifier(table)))
            )
            for row in rows:
                if table == "document":
                    row["latest_document_version"] = None
                selected = [name for name in columns if name in row]
                values = [
                    Jsonb(row[name]) if isinstance(row[name], (dict, list)) else row[name]
                    for name in selected
                ]
                c.execute(
                    sql.SQL("INSERT INTO public.{}({}) VALUES({})").format(
                        sql.Identifier("seal_" + table),
                        sql.SQL(",").join(map(sql.Identifier, selected)),
                        sql.SQL(",").join(sql.Placeholder() for _ in selected),
                    ),
                    values,
                )
        for doc in h.sql("SELECT id,latest_document_version FROM seal.document"):
            c.execute(
                "UPDATE public.seal_document SET latest_document_version=%s WHERE id=%s",
                (doc["latest_document_version"], doc["id"]),
            )
        c.execute("""INSERT INTO public.seal_record(id,source_id,namespace,record_type,record_key)
                      VALUES('retired-record','all','runtime','old-business','historical-key')""")
        binding = c.execute(
            "SELECT id FROM public.seal_binding WHERE source_id='all' LIMIT 1"
        ).fetchone()["id"]
        c.execute(
            """INSERT INTO public.seal_record_result(id,record_id,binding_id,processing_key,output_hash,content_hash,inputs,candidate,checks)
            VALUES('retired-result','retired-record',%s,'old-key','old-hash','old-content','[]','{}','{}')""",
            (binding,),
        )
        run = c.execute(
            "SELECT id,attempt_epoch FROM public.seal_run WHERE source_id='all' AND status='complete' LIMIT 1"
        ).fetchone()
        obs = c.execute(
            "SELECT id FROM public.seal_fetch_observation WHERE run_id=%s LIMIT 1", (run["id"],)
        ).fetchone()["id"]
        c.execute(
            """INSERT INTO public.seal_record_version(id,record_id,binding_id,run_id,attempt_epoch,content_hash,schema_version,data,change_reason)
            VALUES('retired-version','retired-record',%s,%s,%s,'old-content','old-schema','{}','first_seen')""",
            (binding, run["id"], run["attempt_epoch"]),
        )
        c.execute(
            """INSERT INTO public.seal_record_emission(id,run_id,attempt_epoch,record_id,result_id,observation_id,inputs)
            VALUES('retired-emission',%s,%s,'retired-record','retired-result',%s,'[]')""",
            (run["id"], run["attempt_epoch"], obs),
        )
        c.execute(
            "CREATE VIEW public.external_record_dependency AS SELECT id FROM public.seal_record"
        )
        c.execute(
            """INSERT INTO public.seal_run(id,source_id,binding_id,mode,namespace,generation,run_seq,deadline,status,seeds,report)
            SELECT 'historical-replay',source_id,binding_id,'replay',namespace,generation,run_seq,deadline,'complete','[]','{"historical":true}'
            FROM public.seal_run WHERE id=%s""",
            (run["id"],),
        )
    h.env["SEAL_DATABASE_URL"] = old_dsn
    try:
        # Install the pre-existing third-party schema and a completed queue job.
        setup = subprocess.run(
            [
                __import__("sys").executable,
                "-c",
                "from seal.queue import app, crawl_job; "
                "\nwith app.open():\n app.schema_manager.apply_schema()\n crawl_job.defer(run_id='historical-complete')",
            ],
            env=h.env,
            capture_output=True,
        )
        if setup.returncode:
            raise RuntimeError(setup.stderr.decode())
        h.sql("UPDATE public.procrastinate_jobs SET status='succeeded'")
        third_party = h.sql(
            "SELECT id,task_name,status::text,args FROM public.procrastinate_jobs ORDER BY id"
        )
        h.check(
            "old_schema_collect_rejected",
            h.cli("collect", h.output / "config/all.yaml", exit=2)["error"],
            "database_init_required",
        )
        preflight = h.cli("init", exit=2)
        h.check("upgrade_preflight", preflight["status"], "upgrade_confirmation_required")
        deletion = {
            o["name"]: o["rows"]
            for o in preflight["inventory"]["objects"]
            if o["action"] == "delete" and "rows" in o
        }
        h.check(
            "record_rows_listed",
            {
                name: deletion[name]
                for name in [
                    "seal_record",
                    "seal_record_result",
                    "seal_record_version",
                    "seal_record_emission",
                ]
            },
            {
                name: 1
                for name in [
                    "seal_record",
                    "seal_record_result",
                    "seal_record_version",
                    "seal_record_emission",
                ]
            },
        )
        kept = {
            table: h.sql(
                sql.SQL("SELECT count(*) AS n FROM public.{}").format(
                    sql.Identifier("seal_" + table)
                )
            )[0]["n"]
            for table in TABLES
        }
        blocked = h.cli(
            "init", "--backup", h.work / "blocked.dump", "--acknowledge-data-loss", exit=2
        )
        h.check("external_dependency_restrict", blocked["category"], "DependentObjectsStillExist")
        h.check(
            "failed_upgrade_atomic",
            h.sql("SELECT count(*) AS n FROM public.seal_record")[0]["n"],
            1,
        )
        h.sql("DROP VIEW public.external_record_dependency")
        upgraded = h.cli(
            "init", "--backup", h.work / "before-upgrade.dump", "--acknowledge-data-loss"
        )
        h.capture("migration", upgraded)
        h.check(
            "historical_replay_readable",
            h.sql("SELECT mode,status,report FROM seal.run WHERE id='historical-replay'")[0],
            {"mode": "replay", "status": "complete", "report": {"historical": True}},
        )
        for table, count in kept.items():
            h.check(
                "migration_preserves_" + table,
                h.sql(sql.SQL("SELECT count(*) AS n FROM seal.{}").format(sql.Identifier(table)))[
                    0
                ]["n"],
                count,
            )
        h.check(
            "record_tables_physically_absent",
            h.sql(
                "SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('public','seal') AND c.relname LIKE '%record%'"
            )[0]["n"],
            0,
        )
        h.check(
            "old_tables_absent",
            h.sql(
                "SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname LIKE 'seal\\_%'"
            )[0]["n"],
            0,
        )
        h.check(
            "old_prefix_absent",
            h.sql(
                "SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='seal' AND c.relname LIKE 'seal\\_%'"
            )[0]["n"],
            0,
        )
        h.check(
            "constraint_names_clean",
            h.sql(
                "SELECT count(*) AS n FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='seal' AND c.conname LIKE 'seal\\_%'"
            )[0]["n"],
            0,
        )
        h.check(
            "queue_still_public",
            h.sql("SELECT to_regclass('public.procrastinate_jobs')::text AS name")[0]["name"],
            "procrastinate_jobs",
        )
        h.check(
            "queue_rows_untouched",
            h.sql(
                "SELECT id,task_name,status::text,args FROM public.procrastinate_jobs ORDER BY id"
            ),
            third_party,
        )
        h.cli("collect", h.output / "config/all.yaml", "--enqueue")
        h.cli("worker", "--once")
        h.cli("recheck", h.output / "config/all.yaml")
        with psycopg.connect(old_dsn, autocommit=True) as c:
            c.execute("CREATE DATABASE restored")
        restored_dsn = original.rsplit("/", 1)[0] + "/restored"
        subprocess.run(
            [
                "pg_restore",
                "--dbname",
                restored_dsn,
                "--no-owner",
                str(h.work / "before-upgrade.dump"),
            ],
            capture_output=True,
            check=True,
        )
        with psycopg.connect(restored_dsn) as c:
            h.check(
                "backup_restore_record",
                c.execute("SELECT count(*) FROM public.seal_record").fetchone()[0],
                1,
            )
            h.check(
                "backup_restore_documents",
                c.execute("SELECT count(*) FROM public.seal_document_version").fetchone()[0],
                kept["document_version"],
            )
            h.check(
                "backup_restore_queue",
                c.execute("SELECT count(*) FROM public.procrastinate_jobs").fetchone()[0],
                len(third_party),
            )
        h.capture(
            "catalog",
            h.sql("""SELECT n.nspname,c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='seal' OR (n.nspname='public' AND c.relname LIKE 'procrastinate\\_%') ORDER BY n.nspname,c.relname"""),
        )
    finally:
        h.env["SEAL_DATABASE_URL"] = original
