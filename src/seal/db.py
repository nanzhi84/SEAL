import os
import pwd
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .core import SealError, uid

j = Jsonb


def dsn():
    value = os.environ.get("SEAL_DATABASE_URL")
    if not value:
        raise SealError("SEAL_DATABASE_URL_required")
    return value


@contextmanager
def connect():
    with psycopg.connect(
        dsn(),
        row_factory=dict_row,
        connect_timeout=5,
        options="-c statement_timeout=15000 -c lock_timeout=5000",
    ) as connection:
        yield connection


def one(connection, query, params=()):
    value = connection.execute(query, params).fetchone()
    if value is None:
        raise SealError("record_not_found")
    return value


def decision(connection, source, kind, payload, key=None):
    identity = uid()
    value = connection.execute(
        "INSERT INTO seal_decision(id, source_id, kind, actor, payload, idempotency_key) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(idempotency_key) DO NOTHING RETURNING id",
        (identity, source, kind, pwd.getpwuid(os.getuid()).pw_name, j(payload), key),
    ).fetchone()
    return (
        value["id"]
        if value
        else one(connection, "SELECT id FROM seal_decision WHERE idempotency_key=%s", (key,))["id"]
    )


def locked_run(connection, run_id):
    # Every current-state writer uses Source -> Run -> Document lock order.
    source_id = one(connection, "SELECT source_id FROM seal_run WHERE id=%s", (run_id,))[
        "source_id"
    ]
    source = one(connection, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (source_id,))
    run = one(connection, "SELECT * FROM seal_run WHERE id=%s FOR UPDATE", (run_id,))
    return source, run


def fenced(source, run, epoch):
    if run["attempt_epoch"] != epoch or run["deadline"] <= datetime.now(timezone.utc):
        raise SealError("stale_attempt")
    if run["status"] not in ("running", "finishing"):
        raise SealError("inactive_attempt")
    if run["mode"] in ("production", "recheck"):
        if (
            source["generation"] != run["generation"]
            or source["write_seq"] != run["run_seq"]
            or source["paused"]
            or source["binding_id"] != run["binding_id"]
        ):
            raise SealError("superseded")


def record_error(run_id, epoch, code):
    with connect() as connection:
        connection.execute(
            "UPDATE seal_run SET errors=errors || %s WHERE id=%s AND attempt_epoch=%s AND status IN ('running','finishing')",
            (j([code]), run_id, epoch),
        )


def migrate():
    from .queue import app

    with connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(72032101)")
        connection.execute(Path(__file__).with_name("schema.sql").read_text())
        installed = connection.execute(
            "SELECT to_regclass('procrastinate_jobs') AS name"
        ).fetchone()["name"]
    if installed is None:
        with app.open():
            app.schema_manager.apply_schema()
    return {"schema": "v1", "queue": "procrastinate"}
