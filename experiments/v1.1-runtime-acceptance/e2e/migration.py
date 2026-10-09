"""Upgrade real V1 tables and preserved synthetic history through the public CLI."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


def fresh_install(h):
    original = h.env["SEAL_DATABASE_URL"]
    with psycopg.connect(original, autocommit=True) as c:
        c.execute("CREATE DATABASE fresh_install")
    try:
        h.env["SEAL_DATABASE_URL"] = original.rsplit("/", 1)[0] + "/fresh_install"
        h.check("empty_database_install", h.cli("db", "migrate")["schema"], "v1.2")
        h.cli("db", "migrate")
        h.config("fresh")
        run = h.cli("run", "fresh", "--binding", h.binding("fresh"))
        h.check("empty_database_candidate_run", run["status"], "complete")
        h.check("empty_database_results", len(h.export("fresh")["documents"]), 2)
    finally:
        h.env["SEAL_DATABASE_URL"] = original
        with psycopg.connect(original, autocommit=True) as c:
            c.execute("DROP DATABASE fresh_install WITH (FORCE)")


def upgrade_history(h):
    fresh_install(h)
    url = h.site.url + "/legacy/one"
    body = b"<h1>First notice</h1><article>Public content A</article>"

    def obj(data):
        raw = (
            data
            if isinstance(data, bytes)
            else json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        )
        key = hashlib.sha256(raw).hexdigest()
        path = h.root / "archive" / "objects" / key[:2] / key[2:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return key

    body_hash = obj(body)
    snapshot = obj(
        {
            "contract": 1,
            "body_hash": body_hash,
            "body_size": len(body),
            "request_url": url,
            "method": "GET",
            "url": url,
            "status": 200,
            "headers": {"Content-Type": ["text/html; charset=utf-8"]},
            "response_type": "HtmlResponse",
            "encoding": "utf-8",
        }
    )
    request_key = hashlib.sha256(
        json.dumps(
            {
                "url": url,
                "method": "GET",
                "body": hashlib.sha256(b"").hexdigest(),
                "role": "detail",
                "headers": {"Accept": "", "Accept-Language": ""},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    inputs = [
        {
            "key": request_key,
            "snapshot_id": snapshot,
            "observation_id": "legacy-observation",
            "role": "detail",
            "logical_url": url,
            "url": url,
            "method": "GET",
            "fetched_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    with psycopg.connect(h.env["SEAL_DATABASE_URL"]) as c:
        c.execute(Path("src/seal/schema.sql").read_text())
        c.execute(
            "INSERT INTO seal_source(id,config,binding_id) VALUES('legacy',%s,'legacy-binding')",
            (Jsonb({"id": "legacy", "expected_urls": [url]}),),
        )
        c.execute(
            "INSERT INTO seal_recipe_version(id,manifest) VALUES('legacy-recipe',%s)",
            (Jsonb({"historical": True}),),
        )
        c.execute(
            "INSERT INTO seal_binding(id,source_id,recipe_version,config,params,fingerprint) VALUES('legacy-binding','legacy','legacy-recipe',%s,'{}','legacy-fingerprint')",
            (Jsonb({"expected_urls": [url]}),),
        )
        for identity, mode, status in (
            ("legacy-run", "trial", "complete"),
            ("legacy-pending", "trial", "pending"),
            ("legacy-production", "production", "pending"),
        ):
            c.execute(
                "INSERT INTO seal_run(id,source_id,binding_id,mode,namespace,generation,run_seq,deadline,seeds,inputs,status,result_ids) VALUES(%s,'legacy','legacy-binding',%s,%s,0,0,now()+interval '1 hour',%s,%s,%s,%s)",
                (
                    identity,
                    mode,
                    mode + ":legacy",
                    Jsonb([{"url": url, "role": "detail"}]),
                    Jsonb(inputs),
                    status,
                    Jsonb(["legacy-result"] if status == "complete" else []),
                ),
            )
        c.execute(
            "INSERT INTO seal_fetch_observation(id,source_id,run_id,attempt_epoch,request_id,request_key,method,url,status,snapshot_id,body_hash,requested_at,fetched_at) VALUES('legacy-observation','legacy','legacy-run',1,'legacy-request',%s,'GET',%s,200,%s,%s,now(),now())",
            (request_key, url, snapshot, body_hash),
        )
        c.execute(
            "INSERT INTO seal_document(id,source_id,namespace,identity,url,latest_revision,latest_observation,current_result,publication_id) VALUES('legacy-document','legacy','production',%s,%s,'legacy-revision','legacy-observation','legacy-result','legacy-publish')",
            (url, url),
        )
        c.execute(
            "INSERT INTO seal_revision(id,document_id,body_hash,snapshot_id,observation_id) VALUES('legacy-revision','legacy-document',%s,%s,'legacy-observation')",
            (body_hash, snapshot),
        )
        c.execute(
            "INSERT INTO seal_result(id,document_id,binding_id,processing_key,inputs,output_hash,candidate,checks) VALUES('legacy-result','legacy-document','legacy-binding','legacy-key',%s,'legacy-hash',%s,'{}')",
            (
                Jsonb([snapshot]),
                Jsonb({"url": url, "title": "First notice", "body": "Public content A"}),
            ),
        )
        for kind in ("review", "publish"):
            c.execute(
                "INSERT INTO seal_decision(id,source_id,kind,actor,payload) VALUES(%s,'legacy',%s,'synthetic',%s)",
                (
                    "legacy-" + kind,
                    kind,
                    Jsonb(
                        {
                            "gold": [{"title": "First notice", "body": "Public content A"}],
                            "binding_id": "legacy-binding",
                        }
                    ),
                ),
            )

    historical_columns = {}

    def preserved():
        with psycopg.connect(h.env["SEAL_DATABASE_URL"]) as c:
            values = {}
            for table in (
                "recipe_version",
                "binding",
                "revision",
                "result",
                "fetch_observation",
                "decision",
                "run",
            ):
                where = " WHERE id='legacy-run'" if table == "run" else " WHERE id LIKE 'legacy-%'"
                if table not in historical_columns:
                    # Freeze the pre-upgrade contract, including every old column.
                    # New nullable migration metadata changes SELECT * row shape,
                    # but must never change any historical value.
                    historical_columns[table] = [
                        row[0]
                        for row in c.execute(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name=%s "
                            "ORDER BY ordinal_position",
                            ("seal_" + table,),
                        )
                    ]
                values[table] = c.execute(
                    sql.SQL("SELECT to_jsonb(t) FROM (SELECT {} FROM {}{} ORDER BY id) t").format(
                        sql.SQL(",").join(map(sql.Identifier, historical_columns[table])),
                        sql.Identifier("seal_" + table),
                        sql.SQL(where),
                    )
                ).fetchall()
            return values

    # Persist jobs before upgrading; the real Worker must acknowledge, not execute, them.
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from seal.db import connect
from seal.queue import app, enqueue
with app.open():
    app.schema_manager.apply_schema()
with connect() as c:
    enqueue(c, 'legacy-pending')
    enqueue(c, 'legacy-production')
""",
        ],
        env=h.env,
        check=True,
        capture_output=True,
    )
    before = preserved()
    migrated = h.cli("db", "migrate")
    h.check("incremental_migration_v11", migrated["schema"], "v1.2")
    h.check("v1_immutable_history_unchanged", preserved(), before)
    state = h.cli("inspect", "source", "legacy")
    h.check("v1_default_paused_for_upgrade", state["source"]["paused"], True)
    h.check(
        "v1_document_id_and_revision_preserved",
        (state["documents"][0]["id"], state["documents"][0]["latest_revision"]),
        ("legacy-document", "legacy-revision"),
    )
    h.check("v1_trial_inspectable", h.cli("inspect", "run", "legacy-run")["run"]["mode"], "trial")
    h.cli("retry", "legacy-run", ok=False)
    for identity in ("legacy-pending", "legacy-production"):
        h.check(identity + "_stopped", h.cli("inspect", "run", identity)["run"]["status"], "failed")
    count = len(h.site.ledger)
    h.start_worker()
    h.wait_worker()
    h.check("legacy_jobs_never_recrawl", len(h.site.ledger), count)
    with psycopg.connect(h.env["SEAL_DATABASE_URL"]) as c:
        jobs = c.execute(
            "SELECT status FROM procrastinate_jobs WHERE task_name='seal.crawl'"
        ).fetchall()
    h.check("legacy_jobs_acknowledged", jobs, [("succeeded",), ("succeeded",)])
    for key in (body_hash, snapshot):
        path = h.root / "archive" / "objects" / key[:2] / key[2:]
        h.check("v1_object_unchanged_" + key, hashlib.sha256(path.read_bytes()).hexdigest(), key)
    h.cli("db", "migrate")
    h.check("migration_idempotent", h.cli("inspect", "source", "legacy")["source"], state["source"])
    with psycopg.connect(h.env["SEAL_DATABASE_URL"]) as c:
        try:
            c.execute(
                "INSERT INTO seal_run(id,source_id,binding_id,mode,namespace,generation,run_seq,deadline,seeds) VALUES('forbidden','legacy','legacy-binding','trial','trial:new',0,0,now(),'[]')"
            )
        except psycopg.errors.CheckViolation:
            c.rollback()
        else:
            raise AssertionError("database accepted new Trial")
    h.check("database_rejects_new_trial", True)
    help_text = subprocess.run(
        [sys.executable, "-m", "seal", "--help"],
        env=h.env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    h.check(
        "obsolete_cli_removed",
        all(
            word not in help_text
            for word in ("trial", "review", "activate", "rollback", "withdraw")
        ),
    )
    h.config("legacy", entries=[url], seed_role="detail")
    binding = h.binding("legacy")
    count = len(h.site.ledger)
    replay = h.cli("replay", "legacy-run", "--binding", binding)
    h.check("v1_archived_inputs_replay_on_v11", replay["status"], "complete")
    h.check("v1_replay_zero_network", len(h.site.ledger), count)
    h.check(
        "v1_replay_json_retained",
        h.cli("export", "legacy", "--run", replay["run_id"])["documents"][0]["body"],
        "Public content A",
    )
    h.select("legacy", binding, 1)
    h.cli("run", "legacy")
    h.check("v11_run_after_upgrade", len(h.export("legacy")["documents"]), 1)
    (h.output / "migration-history.json").write_text(
        json.dumps({"before": before, "after": preserved()}, indent=2, default=str)
    )
