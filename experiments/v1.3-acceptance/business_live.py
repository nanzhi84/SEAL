"""Real source→Binding→Collect→export E2E; requires isolated retained PG/archive.

Before adapter implementation, the missing package must fail. Behavioral gaps:
changed/looping pagination, missing detail, duplicate business key, changing API
total, empty template, incomplete XLS, transport or policy failures cannot pass.
Complete means only the explicit enumerated Resource, never the whole domain.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from business_profiles import PATHS, profile
from live import ROOT, Driver, summarize, write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sources", nargs="+", choices=PATHS, default=list(PATHS))
    parser.add_argument("--params-json", type=Path)
    args = parser.parse_args()
    if not all(os.environ.get(k) for k in ["SEAL_DATABASE_URL", "SEAL_ARCHIVE"]):
        parser.error("isolated SEAL_DATABASE_URL and SEAL_ARCHIVE required")
    args.output.mkdir(parents=True, exist_ok=False)
    driver = Driver(args.output, 3600)
    manifest = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "engine_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "src/seal").glob("*.py"))
        },
        "archive_root": os.environ["SEAL_ARCHIVE"],
        "database_url_recorded": False,
        "sources": [],
    }
    version = driver.cli("recipe", "pack", ROOT / "recipes/business")["recipe_version"]
    for identity in args.sources:
        directory = args.output / identity
        directory.mkdir()
        config = profile(identity)
        params = {
            "source": identity,
            **(json.loads(args.params_json.read_text()) if args.params_json else {}),
        }
        write(directory / "source.json", config)
        write(directory / "params.json", params)
        driver.cli("source", "apply", directory / "source.json")
        binding = driver.cli(
            "binding",
            "create",
            config["id"],
            "--recipe",
            version,
            "--params",
            directory / "params.json",
        )["binding_id"]
        write(directory / "binding.json", driver.cli("inspect", "binding", binding))
        before = driver.cli("inspect", "source", config["id"])
        driver.cli(
            "source",
            "select",
            config["id"],
            "--binding",
            binding,
            "--expect-generation",
            before["source"]["generation"],
        )
        receipt = driver.cli("collect", config["id"], required=False)
        write(directory / "collect.json", receipt)
        run_id = receipt["run_id"]
        run = driver.cli("inspect", "run", run_id)
        exported = driver.cli("export", config["id"], "--run", run_id)
        write(directory / "run.json", run)
        write(directory / "export.json", exported)
        write(directory / "retained-source.json", driver.cli("inspect", "source", config["id"]))
        observed = {
            "research_id": identity,
            "run_id": run_id,
            "source_id": config["id"],
            "recipe_version": version,
            "observation": summarize(run, exported),
        }
        manifest["sources"].append(observed)
        write(args.output / "manifest.json", manifest)
        print(
            json.dumps({"source": identity, **observed["observation"]}, ensure_ascii=False),
            flush=True,
        )
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    write(args.output / "manifest.json", manifest)
    return int(any(s["observation"]["status"] != "complete" for s in manifest["sources"]))


if __name__ == "__main__":
    sys.exit(main())
