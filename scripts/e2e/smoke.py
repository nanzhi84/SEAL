"""Small independent regression campaign using the established acceptance facilities."""

import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

from .harness import Harness
from .smoke_cases import golden, runtime
from .smoke_support import PACK, Smoke, sha, write


def main(args):
    if not args.baseline:
        raise SystemExit("--stage smoke requires --baseline <M3 artifact directory>")
    if args.live_source and not args.live:
        raise SystemExit("--live-source requires --live")
    output = args.output
    if output.exists():
        output /= datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")
    h = Harness(output)
    s = Smoke(h)
    start = time.monotonic()
    error = None
    # Preserve the actual executable inputs, including an uncommitted worktree.
    from pathlib import Path

    for folder in ("src", "scripts", "recipes"):
        shutil.copytree(
            folder,
            output / "executed-code" / folder,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for name in ("uv.lock", "pyproject.toml", ".python-version"):
        shutil.copyfile(name, output / "executed-code" / name)
    try:
        p = subprocess.run(
            [sys.executable, str(PACK / "verify_pack.py")], capture_output=True, text=True
        )
        (output / "pack-verification.txt").write_text(p.stdout + p.stderr)
        h.check("pack_verified_before_execution", p.returncode, 0)
        p = subprocess.run(
            [sys.executable, "scripts/verify_artifacts.py", str(args.baseline)],
            capture_output=True,
            text=True,
        )
        (output / "baseline-verification.json").write_text(p.stdout)
        h.check("baseline_evidence_integrity", p.returncode, 0)
        baseline_manifest = json.loads((args.baseline / "manifest.json").read_text())
        current_engine = {
            str(p): sha(p.read_bytes())
            for p in Path("src").rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        h.check(
            "baseline_matches_runtime_under_test",
            current_engine,
            {k: v for k, v in baseline_manifest["code"].items() if k.startswith("src/")},
        )
        original_expected = sha((PACK / "golden_expected.json").read_bytes())
        shutil.copytree(PACK, output / "fixture-pack", ignore=shutil.ignore_patterns("__pycache__"))
        h.start()
        s.start_server()
        recipe, runs = golden(s)
        from .smoke_compatibility import historical
        from .smoke_locator_contracts import (
            calendar_rejection,
            negative_and_status,
            network_boundaries,
        )

        historical(s)
        negative_and_status(s, recipe)
        network_boundaries(s, recipe)
        calendar_rejection(s, recipe)
        runtime(s, recipe, runs, args.baseline)
        if args.live:
            from .smoke_live import live

            live(s, args.live_source)
        h.check(
            "golden_expected_unchanged",
            sha((PACK / "golden_expected.json").read_bytes()),
            original_expected,
        )
    except Exception as exc:
        error = exc
        print(f"campaign_error: {type(exc).__name__}: {exc}", flush=True)
    finally:
        s.stop_server()
        required = (
            [f"G{n:02d}" for n in range(1, 9)]
            + [f"R{n:02d}" for n in range(1, 13)]
            + [f"N{n:02d}" for n in range(1, 12)]
            + ["C01", "X01"]
        )
        if args.live:
            names = ["live_court", "live_spp", "live_python"]
            required += [
                f"L{n:02d}"
                for n, name in enumerate(names, 1)
                if not args.live_source or name in args.live_source
            ]
        for identity in required:
            if identity not in {c["id"] for c in s.cases}:
                s.cases.append(
                    {
                        "id": identity,
                        "name": "Not reached",
                        "status": "UNVERIFIED",
                        "assertions": [],
                        "elapsed_seconds": 0,
                    }
                )
        write(output / "cases.json", s.cases)
        # Preserve database state as inspectable SQL, isolated data only.
        if h.pg_started:
            p = subprocess.run(
                ["pg_dump", h.env["SEAL_DATABASE_URL"], "--no-owner", "--no-privileges"],
                capture_output=True,
            )
            if p.returncode == 0:
                (output / "database.sql").write_bytes(p.stdout)
        write(
            output / "summary.json",
            {
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "cases": [{k: r[k] for k in ("id", "status", "elapsed_seconds")} for r in s.cases],
                "quality_status": "not_evaluated",
                "error": str(error) if error else None,
            },
        )
        h.save("smoke", error)
        manifest = json.loads((output / "manifest.json").read_text())
        manifest["command"] = (
            "./scripts/acceptance.sh --stage smoke --baseline "
            f"{args.baseline} --output <new-directory>"
            + (" --live" if args.live else "")
            + "".join(f" --live-source {name}" for name in args.live_source or [])
        )
        manifest["baseline"] = str(args.baseline)
        manifest["scope"] = "isolated Runtime engineering fixtures; optional bounded public sources"
        manifest["unverified"] = [
            "Business quality/completeness",
            "Power loss durability",
            "Production scale",
            "Untrusted Python isolation",
        ]
        write(output / "manifest.json", manifest)
        h.close()
    print(output.resolve())
    return int(error is not None or any(c["status"] == "FAIL" for c in s.cases))
