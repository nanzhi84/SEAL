"""Freeze a Source configuration and create its operational Run in one transaction."""

from .config import load_file
from .core import SealError, digest, uid
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
            # A new parsing configuration must not change the input mode of an existing timeline.
            if (
                frontier
                and c.execute(
                    "SELECT 1 FROM seal.version v JOIN seal.resource r ON r.id=v.resource_id WHERE r.frontier_id=%s AND v.input_mode<>%s LIMIT 1",
                    (frontier["id"], config["crawl4ai"]["mode"]),
                ).fetchone()
            ):
                raise SealError("input_mode_change_requires_new_source")
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
