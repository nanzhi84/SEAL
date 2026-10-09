"""Read historical immutable rows and replay their archived inputs in an isolated copy."""

import base64
import json
import shutil
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql

from .smoke_cases import semantic
from .smoke_support import offline_guard, sha, write


def historical(s, compatibility_baseline=None):
    h = s.h
    evidence = compatibility_baseline or Path("artifacts/acceptance/v1.1-smoke/court-confirmed")
    with s.case(
        "C01", "Previous Recipe/Binding/Result format and historical input compatibility"
    ) as case:
        if not (evidence / "database.sql").is_file():
            if compatibility_baseline is not None:
                h.check("explicit_historical_database_present", False)
            case["reason"] = (
                "Previous acceptance database unavailable; generate the prior baseline first"
            )
            return
        h.check("historical_archive_present", (evidence / "archive/objects").is_dir())
        declaration = evidence / "manifest.json"
        if declaration.is_file():
            declared = json.loads(declaration.read_text())["files"]
        else:
            declaration = evidence / "provenance.json"
            h.check("historical_hash_declaration_present", declaration.is_file())
            declared = json.loads(declaration.read_text())["copied_files_sha256"]
        h.check("historical_sql_hash_declared", "database.sql" in declared)
        h.check(
            "historical_input_hashes_verified",
            bool(declared)
            and all(
                (evidence / name).resolve().is_relative_to(evidence.resolve())
                and (evidence / name).is_file()
                and sha((evidence / name).read_bytes()) == expected
                for name, expected in declared.items()
            ),
        )
        h.check(
            "historical_objects_content_addressed",
            all(
                sha(path.read_bytes()) == path.parent.name + path.name
                for path in (evidence / "archive/objects").glob("*/*")
                if path.is_file()
            ),
        )
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
                h.check(
                    "historical_schema_predates_records",
                    connection.execute("SELECT to_regclass('seal_record')").fetchone()[0],
                    None,
                )
                run_id, binding = connection.execute(
                    "SELECT id,binding_id FROM seal_run WHERE source_id='golden_g01' AND mode='collect' AND status='complete' ORDER BY created_at LIMIT 1"
                ).fetchone()
                columns = {}
                for table in (
                    "recipe_version",
                    "binding",
                    "run",
                    "document",
                    "revision",
                    "result",
                    "fetch_observation",
                    "decision",
                ):
                    columns[table] = [
                        row[0]
                        for row in connection.execute(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                            ("seal_" + table,),
                        )
                    ]

            def historical_values():
                with psycopg.connect(h.env["SEAL_DATABASE_URL"]) as connection:
                    return {
                        table: connection.execute(
                            sql.SQL(
                                "SELECT to_jsonb(t) FROM (SELECT {} FROM {} ORDER BY id) t"
                            ).format(
                                sql.SQL(",").join(map(sql.Identifier, names)),
                                sql.Identifier("seal_" + table),
                            )
                        ).fetchall()
                        for table, names in columns.items()
                    }

            # Current code writes the current schema: follow the public upgrade
            # path before making a new Binding/Replay, preserving all old values.
            old_values = historical_values()
            h.check("legacy_export_before_upgrade", len(h.export("golden_g01")["documents"]), 1)
            h.check("historical_public_migration", h.cli("db", "migrate")["schema"], "v1.2")
            h.check("historical_old_columns_unchanged", historical_values(), old_values)
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
                    "historical_input_path": str(evidence),
                    "historical_declaration": str(declaration),
                    "historical_declaration_sha256": sha(declaration.read_bytes()),
                },
            )
        finally:
            h.env.clear()
            h.env.update(original_env)
            with psycopg.connect(h.env["SEAL_DATABASE_URL"], autocommit=True) as connection:
                connection.execute("DROP DATABASE compatibility WITH (FORCE)")
