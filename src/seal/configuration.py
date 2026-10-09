"""Minimal operator configuration, not review or publication governance."""

from .config import SourceConfig
from .core import SealError, digest
from .db import connect, decision, j, one


def register_source(data):
    config = SourceConfig.model_validate(data)
    with connect() as c:
        c.execute(
            "INSERT INTO seal_source(id, config) VALUES(%s,%s) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
            (config.id, j(config.model_dump())),
        )
        decision(
            c,
            config.id,
            "control",
            {"action": "source_apply", "config_hash": digest(config.model_dump())},
        )
    return {"source_id": config.id}


def select_binding(source_id, binding_id, generation):
    from .recipes import load_bundle

    with connect() as c:
        source = one(c, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (source_id,))
        binding = one(c, "SELECT * FROM seal_binding WHERE id=%s", (binding_id,))
        if binding["source_id"] != source_id:
            raise SealError("binding_source_mismatch")
        if source["generation"] != generation:
            raise SealError("generation_conflict")
        load_bundle(binding["recipe_version"])
        c.execute(
            "UPDATE seal_source SET binding_id=%s,generation=generation+1,paused=false,next_poll=now() WHERE id=%s",
            (binding_id, source_id),
        )
        identity = decision(
            c,
            source_id,
            "control",
            {"action": "select_binding", "binding_id": binding_id, "generation": generation + 1},
        )
    return {"control_id": identity, "binding_id": binding_id, "generation": generation + 1}


def pause(source_id, reason):
    with connect() as c:
        source = one(c, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (source_id,))
        c.execute(
            "UPDATE seal_source SET paused=true,generation=generation+1 WHERE id=%s", (source_id,)
        )
        decision(
            c,
            source_id,
            "control",
            {"action": "pause", "reason": reason, "generation": source["generation"] + 1},
        )
    return {"source_id": source_id, "paused": True, "generation": source["generation"] + 1}
