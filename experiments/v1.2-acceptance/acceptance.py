"""Joint Runtime acceptance through real CLI, Scrapy, PostgreSQL and Worker paths."""

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/v1.1-runtime-acceptance"))

from e2e.harness import Harness as LegacyHarness  # noqa: E402

SITE_SPEC = importlib.util.spec_from_file_location(
    "seal_v12_site", Path(__file__).with_name("site.py")
)
SITE_MODULE = importlib.util.module_from_spec(SITE_SPEC)
sys.modules[SITE_SPEC.name] = SITE_MODULE
SITE_SPEC.loader.exec_module(SITE_MODULE)
Site = SITE_MODULE.Site

from test_runtime import run_all  # noqa: E402


class Harness(LegacyHarness):
    def __init__(self, output):
        super().__init__(output)
        self.site.close()
        self.site = Site()

    def start(self):
        # This fixture owns its PG cluster; inherited external DSNs were removed by Harness.
        for tool in ("initdb", "pg_ctl"):
            if not shutil.which(tool):
                raise RuntimeError(f"Missing prerequisite: {tool} (PostgreSQL 17+)")
        subprocess.run(
            ["initdb", "-D", str(self.pg), "-A", "trust", "--no-locale", "--encoding=UTF8"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "pg_ctl",
                "-D",
                str(self.pg),
                "-l",
                str(self.root / "pg.log"),
                "-o",
                f"-h 127.0.0.1 -p {self.port} -k {self.root}",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        self.pg_started = True
        self.cli("db", "migrate")

    def capture(self, name, data):
        (self.output / (name + ".json")).write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str)
        )

    def check(self, name, actual, expected=True):
        # Persist JSON values, not unordered Python set representations.
        def frozen(value):
            if isinstance(value, set):
                return sorted(frozen(item) for item in value)
            if isinstance(value, (list, tuple)):
                return [frozen(item) for item in value]
            if isinstance(value, dict):
                return {key: frozen(item) for key, item in value.items()}
            return value

        return super().check(name, frozen(actual), frozen(expected))

    def save(self, stage, error=None):
        super().save(stage, error)
        path = self.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.update(
            version="v1.2",
            command=(
                "uv run --frozen python experiments/v1.2-acceptance/acceptance.py "
                "--only public-probe --output <new-directory>"
                if stage == "public-probe-security"
                else "./experiments/v1.2-acceptance/acceptance.sh --output <new-directory>"
            ),
            code={
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for folder in ("src", "experiments/v1.2-acceptance", "recipes")
                for p in Path(folder).rglob("*")
                if p.is_file()
                and "__pycache__" not in p.parts
                and not p.is_relative_to(Path("experiments/v1.2-acceptance/results"))
            },
            scope=(
                "Public probe metadata privacy and refusal-stop contracts: isolated loopback fixtures"
                if stage == "public-probe-security"
                else "Runtime synthetic contracts: isolated PostgreSQL + loopback sources"
            ),
            real_source_acceptance={
                "status": "NOT_INCLUDED",
                "reason": "Real A/B/C evidence is validated separately; synthetic PASS does not satisfy T7S",
            },
        )
        if stage == "public-probe-security":
            probe = ROOT / "experiments/source-accessibility/probe.py"
            manifest["code"][str(probe.relative_to(ROOT))] = hashlib.sha256(
                probe.read_bytes()
            ).hexdigest()
        path.write_text(json.dumps(manifest, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--only", choices=["public-probe"], help="Run one existing Harness contract group"
    )
    args = parser.parse_args()
    output = args.output
    if output.exists():
        output = output / datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")
    h = Harness(output)
    error = None
    stage = "public-probe-security" if args.only else "joint-runtime"
    try:
        h.start()
        if args.only == "public-probe":
            sys.path.insert(0, str(ROOT / "experiments/source-accessibility"))
            from public_probe_acceptance import run_public_probe

            run_public_probe(h)
        else:
            run_all(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save(stage, error)
        h.close()
    print(output.resolve())
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
