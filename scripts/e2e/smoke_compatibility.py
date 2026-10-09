"""Read historical immutable rows and replay their archived inputs in an isolated copy."""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import psycopg

from .smoke_cases import semantic
from .smoke_support import offline_guard, sha, write


def historical(s):
    h = s.h
    evidence = Path("artifacts/acceptance/v1.1-smoke/court-confirmed")
    with s.case(
        "C01", "Previous Recipe/Binding/Result format and historical input compatibility"
    ) as case:
        if not (evidence / "database.sql").is_file():
            case["reason"] = (
                "Previous acceptance database unavailable; generate the prior baseline first"
            )
            return
        original_env = dict(h.env)
        with psycopg.connect(h.env["SEAL_DATABASE_URL"], autocommit=True) as connection:
            connection.execute("CREATE DATABASE compatibility")
        try:
            h.env["SEAL_DATABASE_URL"] = (
                h.env["SEAL_DATABASE_URL"].rsplit("/", 1)[0] + "/compatibility"
            )
            restore = subprocess.run(
                [
                    "psql",
                    "-X",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-d",
                    h.env["SEAL_DATABASE_URL"],
                    "-f",
                    str(evidence / "database.sql"),
                ],
                capture_output=True,
            )
            h.check("historical_database_restored", restore.returncode, 0)
            archive = h.root / "legacy-archive"
            shutil.copytree(evidence / "archive", archive)
            h.env["SEAL_ARCHIVE"] = str(archive)
            with psycopg.connect(h.env["SEAL_DATABASE_URL"]) as connection:
                run_id, binding = connection.execute(
                    "SELECT id,binding_id FROM seal_run WHERE source_id='golden_g01' AND mode='collect' AND status='complete' ORDER BY created_at LIMIT 1"
                ).fetchone()
            before_binding = h.cli("inspect", "binding", binding)
            before_run = h.cli("inspect", "run", run_id)
            before_export = h.cli("export", "golden_g01", "--run", run_id)
            h.check("legacy_result_readable", len(before_export["documents"]), 1)
            key = before_binding["recipe_version"]
            bundle = json.loads((archive / "objects" / key[:2] / key[2:]).read_text())
            package = h.root / "legacy-recipe"
            package.mkdir()
            for relative, encoded in bundle["files"].items():
                target = package / relative
                if not target.resolve().is_relative_to(package.resolve()):
                    raise ValueError("unsafe_historical_package_path")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(encoded))
            version = h.cli("recipe", "pack", package)["recipe_version"]
            replacement = h.binding("golden_g01", recipe=version, params=before_binding["params"])
            objects = {
                str(p.relative_to(archive)): sha(p.read_bytes())
                for p in (archive / "objects").glob("*/*")
            }
            with offline_guard(h):
                replay = h.cli("replay", run_id, "--binding", replacement)
                after_export = h.cli("export", "golden_g01", "--run", replay["run_id"])
                h.check("legacy_recipe_repacked_replay_complete", replay["status"], "complete")
                h.check(
                    "legacy_locator_results_equal",
                    semantic(after_export["documents"]),
                    semantic(before_export["documents"]),
                )
                h.check(
                    "legacy_replay_zero_observations",
                    h.cli("inspect", "run", replay["run_id"])["observations"],
                    [],
                )
            h.check(
                "legacy_binding_unchanged", h.cli("inspect", "binding", binding), before_binding
            )
            h.check("legacy_run_results_unchanged", h.cli("inspect", "run", run_id), before_run)
            h.check(
                "legacy_export_unchanged",
                h.cli("export", "golden_g01", "--run", run_id),
                before_export,
            )
            h.check(
                "legacy_objects_unchanged",
                {key: sha((archive / key).read_bytes()) for key in objects},
                objects,
            )
            write(
                h.output / "legacy-compatibility.json",
                {
                    "binding_before": before_binding,
                    "run_before": before_run,
                    "old_export": before_export,
                    "replay": replay,
                    "new_export": after_export,
                    "historical_sql_sha256": sha((evidence / "database.sql").read_bytes()),
                },
            )
        finally:
            h.env.clear()
            h.env.update(original_env)
            with psycopg.connect(h.env["SEAL_DATABASE_URL"], autocommit=True) as connection:
                connection.execute("DROP DATABASE compatibility WITH (FORCE)")
