import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from .site import Site


class Harness:
    def __init__(self, output):
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.temp = tempfile.TemporaryDirectory(prefix="seal-e2e-")
        self.root = Path(self.temp.name)
        self.env = dict(os.environ)
        self.env.pop("SEAL_DATABASE_URL", None)
        self.env.pop("SEAL_ARCHIVE", None)
        self.env["SEAL_ARCHIVE"] = str(self.root / "archive")
        self.env["SEAL_ALLOW_LOOPBACK"] = "1"
        self.assertions, self.receipts = [], []
        self.site = Site()
        self.worker = None
        self.pg_started = False
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.pg = self.root / "pg"
        self.env["SEAL_DATABASE_URL"] = f"postgresql://127.0.0.1:{port}/postgres"
        self.port = port

    def start(self):
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
        from .migration import upgrade_history

        upgrade_history(self)

    def cli(self, *args, ok=True):
        p = subprocess.run(
            [sys.executable, "-m", "seal", *map(str, args)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        try:
            data = json.loads(p.stdout)
        except ValueError:
            data = {"invalid_stdout": p.stdout[-1000:], "stderr": p.stderr[-2000:]}
        self.receipts.append({"args": list(map(str, args)), "exit": p.returncode, "result": data})
        if ok and p.returncode:
            raise AssertionError(f"CLI {args}: {data}; stderr={p.stderr[-2000:]}")
        if not ok and not p.returncode:
            raise AssertionError(f"CLI should reject {args}: {data}")
        return data

    def check(self, name, actual, expected=True):
        passed = actual == expected
        self.assertions.append(
            {
                "name": name,
                "expected": expected,
                "actual": actual,
                "status": "PASS" if passed else "FAIL",
                "evidence": "receipts.json",
            }
        )
        if not passed:
            raise AssertionError(name)

    def config(self, source="a", entries=None, **extra):
        data = {
            "id": source,
            "entry_urls": entries or [self.site.url + f"/{source}/list"],
            "allowed_hosts": ["127.0.0.1"],
            "allowed_path_prefixes": ["/"],
            "archive_approved": True,
            "scope": "Authorized synthetic loopback fixtures",
            "poll_seconds": 3600,
            "recheck_seconds": 3600,
            "budget": {"requests": 40, "seconds": 25, "response_bytes": 100000},
        }
        data.update(extra)
        path = self.root / f"{source}.json"
        path.write_text(json.dumps(data))
        self.cli("source", "apply", path)
        return data

    def binding(self, source="a", recipe=None, params=None):
        if recipe is None:
            recipe = self.cli("recipe", "pack", "recipes/generic")["recipe_version"]
        path = self.root / f"{source}-params.json"
        path.write_text(json.dumps(params or {}))
        return self.cli("binding", "create", source, "--recipe", recipe, "--params", path)[
            "binding_id"
        ]

    def select(self, source, binding, generation):
        return self.cli(
            "source", "select", source, "--binding", binding, "--expect-generation", generation
        )

    def export(self, source="a"):
        return self.cli("export", source)

    def start_worker(self):
        self.worker_log = (self.root / "worker.log").open("w")
        self.worker = subprocess.Popen(
            [sys.executable, "-m", "seal", "worker", "--once"],
            env=self.env,
            stdout=self.worker_log,
            stderr=self.worker_log,
            start_new_session=True,
        )

    def wait_worker(self):
        self.worker.wait(timeout=90)
        self.worker_log.close()
        if self.worker.returncode:
            raise AssertionError((self.root / "worker.log").read_text()[-3000:])
        self.worker = None

    def save(self, stage, error=None):
        if error:
            self.assertions.append(
                {
                    "name": "suite_completed",
                    "expected": "completed",
                    "actual": str(error),
                    "status": "FAIL",
                }
            )
        for name, data in {
            "assertions": self.assertions,
            "receipts": self.receipts,
            "request-ledger": self.site.ledger,
        }.items():
            (self.output / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))
        archive = self.root / "archive"
        if archive.exists():
            shutil.copytree(archive, self.output / "archive")
        files = {
            str(p.relative_to(self.output)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.output.rglob("*")
            if p.is_file()
        }
        manifest = {
            "version": "v1.1",
            "stage": stage,
            "command": f"./experiments/v1.1-runtime-acceptance/acceptance.sh --stage {stage} --output <new-directory>",
            "python": sys.version,
            "uv_lock": hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest(),
            "code": {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for folder in ("src", "experiments/v1.1-runtime-acceptance", "recipes")
                for p in Path(folder).rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            },
            "files": files,
            "scope": "isolated PostgreSQL + loopback synthetic source",
            "unverified": [
                "Real authorized sources and human source completeness review",
                "Power loss durability",
            ],
        }
        (self.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
        passed = sum(a["status"] == "PASS" for a in self.assertions)
        (self.output / "report.txt").write_text(
            f"{stage}: {passed}/{len(self.assertions)} PASS\nSynthetic acceptance only. See manifest.json and assertions.json.\n"
        )

    def close(self):
        if self.worker and self.worker.poll() is None:
            import signal

            os.killpg(self.worker.pid, signal.SIGTERM)
            self.worker.wait(timeout=15)
            self.worker_log.close()
        self.site.close()
        if self.pg_started:
            subprocess.run(
                ["pg_ctl", "-D", str(self.pg), "-m", "immediate", "-w", "stop"], capture_output=True
            )
        self.temp.cleanup()
