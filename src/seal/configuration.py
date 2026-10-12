"""A Source file atomically selects an immutable Binding and creates its Run."""

from pathlib import Path

import jsonschema

from .config import CollectionConfig, load_file
from .core import SealError, digest
from .db import connect, decision, j, one
from .recipes import load_bundle, pack_recipe


def prepare_run(path, mode, *, enqueue=False):
    from .runs import create_run

    path = Path(path).resolve()
    config = CollectionConfig.model_validate(load_file(path))
    recipe_path = (path.parent / config.recipe.path).resolve()
    version = pack_recipe(recipe_path)["recipe_version"]
    bundle = load_bundle(version)
    params = config.recipe.params
    try:
        jsonschema.validate(params, bundle["manifest"]["params_schema"])
    except jsonschema.ValidationError:
        raise SealError("invalid_recipe_params") from None
    data = config.model_dump(exclude={"recipe"})
    execution = config.execution()
    fingerprint = digest(
        {"config": execution, "params": params, "recipe_version": version, "contract": 3}
    )
    with connect() as c:
        c.execute(
            "INSERT INTO seal.source(id,config) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (config.id, j(data)),
        )
        source = one(c, "SELECT * FROM seal.source WHERE id=%s FOR UPDATE", (config.id,))
        c.execute(
            "INSERT INTO seal.binding(id,source_id,recipe_version,config,params,fingerprint) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (fingerprint, config.id, version, j(execution), j(params), fingerprint),
        )
        if source["binding_id"] != fingerprint:
            c.execute(
                "UPDATE seal.source SET binding_id=%s,config=%s,generation=generation+1 WHERE id=%s",
                (fingerprint, j(data), config.id),
            )
            decision(
                c,
                config.id,
                "control",
                {
                    "action": "select_configuration",
                    "binding_id": fingerprint,
                    "generation": source["generation"] + 1,
                },
            )
        elif source["config"] != data:
            c.execute("UPDATE seal.source SET config=%s WHERE id=%s", (j(data), config.id))
        run_id = create_run(fingerprint, mode, c)
        if enqueue:
            from .queue import enqueue as submit

            submit(c, run_id)
    return run_id
