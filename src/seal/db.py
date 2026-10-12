import os
import pwd
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .core import SealError, uid
from .run_state import eligibility_error

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
        raise SealError("row_not_found")
    return value


def decision(connection, source, kind, payload, key=None):
    identity = uid()
    value = connection.execute(
        "INSERT INTO seal.decision(id, source_id, kind, actor, payload, idempotency_key) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(idempotency_key) DO NOTHING RETURNING id",
        (identity, source, kind, pwd.getpwuid(os.getuid()).pw_name, j(payload), key),
    ).fetchone()
    return (
        value["id"]
        if value
        else one(connection, "SELECT id FROM seal.decision WHERE idempotency_key=%s", (key,))["id"]
    )


def locked_run(connection, run_id):
    # Every current-state writer uses Source -> Run -> Document lock order.
    source_id = one(connection, "SELECT source_id FROM seal.run WHERE id=%s", (run_id,))[
        "source_id"
    ]
    source = one(connection, "SELECT * FROM seal.source WHERE id=%s FOR UPDATE", (source_id,))
    run = one(connection, "SELECT * FROM seal.run WHERE id=%s FOR UPDATE", (run_id,))
    return source, run


def fenced(source, run, epoch):
    if run["attempt_epoch"] != epoch:
        raise SealError("stale_attempt")
    if run["deadline"] <= datetime.now(timezone.utc):
        raise SealError("deadline_exceeded")
    if run["status"] not in ("running", "finishing"):
        raise SealError("inactive_attempt")
    code = eligibility_error(source, run)
    if code:
        raise SealError(code)
    if source["write_seq"] != run["run_seq"]:
        raise SealError("run_not_started")


def record_error(run_id, epoch, code):
    with connect() as connection:
        connection.execute(
            "UPDATE seal.run SET errors=errors || %s WHERE id=%s AND attempt_epoch=%s AND status IN ('running','finishing')",
            (j([code]), run_id, epoch),
        )
