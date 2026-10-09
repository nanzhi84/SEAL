"""Immutable, reviewed Python bundles; no build hooks or dependency installation."""

import ast
import base64
import importlib
import importlib.metadata
import platform
import re
import ssl
import sys
from pathlib import Path

import jsonschema
from lxml import etree

from .config import load_file
from .core import Objects, SealError, atomic_write, digest
from .db import connect, j, one


def environment():
    root = Path(__file__).resolve().parents[2]
    lock = root / "uv.lock"
    if not lock.exists():
        raise SealError("environment_lock_missing")
    engine = {p.name: digest(p.read_bytes()) for p in Path(__file__).parent.glob("*.py")}
    engine["schema.sql"] = digest(Path(__file__).with_name("schema.sql").read_bytes())
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "openssl": ssl.OPENSSL_VERSION,
        "libxml": list(etree.LIBXML_VERSION),
        "reactor": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "dependencies": dict(
            sorted(
                (d.metadata["Name"].lower(), d.version) for d in importlib.metadata.distributions()
            )
        ),
        "lock_hash": digest(lock.read_bytes()),
        "engine": engine,
        "archive_contract": 1,
        "validation_contract": 1,
    }


def pack_recipe(path):
    root = Path(path).resolve()
    manifest = load_file(root / "recipe.yaml")
    if not isinstance(manifest, dict) or set(manifest) != {"family", "entrypoint", "params_schema"}:
        raise SealError("invalid_recipe_manifest")
    if not re.fullmatch(r"[a-zA-Z_][\w.]*:[A-Za-z_]\w*", manifest["entrypoint"]):
        raise SealError("invalid_recipe_entrypoint")
    jsonschema.Draft202012Validator.check_schema(manifest["params_schema"])
    files = {}
    for file in sorted(root.rglob("*")):
        if "__pycache__" in file.parts or file.suffix == ".pyc":
            continue
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            raise SealError("recipe_symlink_rejected")
        if not file.is_file():
            continue
        if file.name.startswith(".") or file.suffix in (".env", ".pem", ".key"):
            raise SealError("recipe_private_file_rejected")
        data = file.read_bytes()
        if len(data) > 1048576 or len(files) >= 100:
            raise SealError("recipe_package_too_large")
        if file.suffix == ".py":
            tree = ast.parse(data)
            for node in ast.walk(tree):
                names = (
                    [n.name.split(".")[0] for n in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module.split(".")[0]]
                    if isinstance(node, ast.ImportFrom) and node.module
                    else []
                )
                if set(names) & {"requests", "httpx", "aiohttp", "socket", "subprocess", "urllib"}:
                    raise SealError("recipe_network_helper_requires_review")
        files[file.relative_to(root).as_posix()] = base64.b64encode(data).decode()
    if manifest["entrypoint"].split(":")[0].replace(".", "/") + ".py" not in files:
        raise SealError("recipe_entrypoint_missing")
    env = environment()
    bundle = {"manifest": manifest, "files": files, "environment": env}
    version = Objects().put_json(bundle)
    with connect() as c:
        c.execute(
            "INSERT INTO seal_recipe_version(id,manifest) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (
                version,
                j(
                    {
                        "object": version,
                        "environment": env,
                        "python": sys.executable,
                        "family": manifest["family"],
                    }
                ),
            ),
        )
    return {"recipe_version": version, "environment_digest": digest(env)}


def load_bundle(version):
    with connect() as c:
        row = one(c, "SELECT * FROM seal_recipe_version WHERE id=%s", (version,))
    bundle = Objects().json(row["manifest"]["object"])
    if bundle["environment"] != environment():
        raise SealError("environment_drift")
    return bundle


def import_recipe(version):
    bundle = load_bundle(version)
    root = Objects().root / "packages" / version
    for relative, encoded in bundle["files"].items():
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise SealError("recipe_path_escape")
        data = base64.b64decode(encoded)
        if path.exists() and path.read_bytes() != data:
            raise SealError("recipe_materialization_corrupt")
        if not path.exists():
            atomic_write(path, data)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    module, name = bundle["manifest"]["entrypoint"].split(":")
    return getattr(importlib.import_module(module), name)


def create_binding(source_id, version, params):
    bundle = load_bundle(version)
    try:
        jsonschema.validate(params, bundle["manifest"]["params_schema"])
    except jsonschema.ValidationError:
        raise SealError("invalid_recipe_params") from None
    from .config import SourceConfig

    with connect() as c:
        source = one(c, "SELECT * FROM seal_source WHERE id=%s FOR UPDATE", (source_id,))
        config = SourceConfig.model_validate(source["config"]).execution()
        fingerprint = digest(
            {"config": config, "params": params, "recipe_version": version, "contract": 1}
        )
        c.execute(
            "INSERT INTO seal_binding(id,source_id,recipe_version,config,params,fingerprint) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (fingerprint, source_id, version, j(config), j(params), fingerprint),
        )
    return {"binding_id": fingerprint, "fingerprint": fingerprint}
