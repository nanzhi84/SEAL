"""Freeze a Source configuration and create its operational Run in one transaction."""

from .config import load_file
from .core import digest, uid
from .db import connect, decision, j, one


def prepare_run(path, mode, *, enqueue=False):
    from .runs import create_run

    config = load_file(path).model_dump()
    fingerprint = digest(config)
    with connect() as c:
        c.execute(
            "INSERT INTO seal.source(id,config) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (config["id"], j(config)),
        )
        source = one(c, "SELECT * FROM seal.source WHERE id=%s FOR UPDATE", (config["id"],))
        frontier = c.execute(
            "SELECT id FROM seal.frontier WHERE source_id=%s", (config["id"],)
        ).fetchone()
        if source["config"] != config:
            # Acquisition kind is recorded per input; a new configuration only selects a
            # new generation and never rewrites existing Versions or their inputs.
            c.execute(
                "UPDATE seal.source SET config=%s,generation=generation+1 WHERE id=%s",
                (j(config), config["id"]),
            )
            decision(
                c,
                config["id"],
                "control",
                {"action": "select_configuration", "config_hash": fingerprint},
            )
        if not frontier:
            c.execute(
                "INSERT INTO seal.frontier(id,source_id) VALUES(%s,%s)", (uid(), config["id"])
            )
        identity = create_run(config["id"], mode, c)
        if enqueue:
            from .queue import enqueue as submit

            submit(c, identity)
    return identity
