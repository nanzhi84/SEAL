"""Real CLI/HTTP/PostgreSQL harness; no inherited production DSN or archive."""

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from site_fixture import Site

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


class Harness:
    def __init__(self, name):
        self.output = BASE / "artifacts" / name
        self.work = BASE / "work" / name
        self.output.mkdir(parents=True, exist_ok=False)
        self.work.mkdir(parents=True, exist_ok=False)
        self.temp = tempfile.TemporaryDirectory(prefix="seal-documents-")
        self.pg = Path(self.temp.name) / "pg"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.env = dict(
            os.environ,
            SEAL_DATABASE_URL=f"postgresql://127.0.0.1:{self.port}/postgres",
            SEAL_ARCHIVE=str(self.work / "archive"),
            SEAL_ALLOW_LOOPBACK="1",
        )
        self.env.pop("SEAL_USE_ENV_PROXY", None)
        self.env.pop("PYTHONPATH", None)
        self.checks, self.commands, self.processes = [], [], []
        self.started = False
        self.scope = "Isolated PostgreSQL and synthetic loopback HTTP; no production data"
        self.command = (
            f"uv run --frozen python experiments/document-only/acceptance.py --name {name}"
        )
        self.site = Site()

    def start(self):
        subprocess.run(
            ["initdb", "-D", str(self.pg), "-A", "trust", "--no-locale", "--encoding=UTF8"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "pg_ctl",
                "-D",
                str(self.pg),
                "-l",
                str(self.work / "pg.log"),
                "-o",
                f"-h 127.0.0.1 -p {self.port} -k {self.temp.name}",
                "-w",
                "start",
            ],
            capture_output=True,
            check=True,
        )
        self.started = True

    def sql(self, query, params=()):
        with psycopg.connect(self.env["SEAL_DATABASE_URL"], row_factory=dict_row) as c:
            cursor = c.execute(query, params or None)
            return cursor.fetchall() if cursor.description else []

    def cli(self, *args, exit=0):
        command = [sys.executable, "-m", "seal", *map(str, args)]
        result = subprocess.run(
            command, env=self.env, capture_output=True, text=True, timeout=120, cwd=ROOT
        )
        try:
            data = json.loads(result.stdout)
        except ValueError:
            data = {"stdout": result.stdout, "stderr": result.stderr}
        self.commands.append(
            {"command": command, "exit": result.returncode, "result": data, "stderr": result.stderr}
        )
        self.check("cli_exit_" + str(len(self.commands)), result.returncode, exit)
        self.check("non_tty_no_ansi_" + str(len(self.commands)), "\x1b" not in result.stderr)
        return data

    def config(self, identity, paths, recipe="documents", params=None, **extra):
        data = {
            "id": identity,
            "entry_urls": [self.site.url + p for p in paths],
            "allowed_hosts": ["127.0.0.1"],
            "allowed_path_prefixes": ["/"],
            "archive_approved": True,
            "scope": "Authorized bounded synthetic public source",
            "robots": True,
            "delay": 0.0,
            "concurrency": 2,
            "budget": {"requests": 120, "seconds": 30, "response_bytes": 10000000},
            "recipe": {"path": str(ROOT / "recipes" / recipe), "params": params or {}},
        }
        data.update(extra)
        path = self.output / "config" / (identity + ".yaml")
        write(path, data)
        return path

    def check(self, name, actual, expected=True):
        passed = actual == expected
        # Round-trip sets and dates before writing bounded evidence.
        self.checks.append(
            {
                "name": name,
                "actual": actual,
                "expected": expected,
                "status": "PASS" if passed else "FAIL",
            }
        )
        write(self.output / "assertions.json", self.checks)
        if not passed:
            raise AssertionError(name)

    def capture(self, name, data):
        write(self.output / (name + ".json"), data)

    def spawn(self, *args):
        path = self.work / f"process-{len(self.processes)}.log"
        log = path.open("w")
        process = subprocess.Popen(
            [sys.executable, "-m", "seal", *map(str, args)],
            env=self.env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        self.processes.append((process, log))
        return process

    def save(self, error=None):
        self.capture("commands", self.commands)
        self.capture("requests", self.site.ledger)
        if self.started:
            subprocess.run(
                [
                    "pg_dump",
                    "--dbname",
                    self.env["SEAL_DATABASE_URL"],
                    "--format=custom",
                    "--file",
                    str(self.work / "final.dump"),
                ],
                capture_output=True,
                check=True,
            )
        code = {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in (ROOT / "src", ROOT / "recipes", BASE)
            for p in folder.rglob("*")
            if p.is_file()
            and p.suffix in (".py", ".sql", ".yaml")
            and "__pycache__" not in p.parts
            and "artifacts" not in p.parts
            and "work" not in p.parts
        }
        files = {
            str(p.relative_to(self.output)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.output.rglob("*")
            if p.is_file() and p.name != "summary.json"
        }
        self.capture(
            "summary",
            {
                "status": "FAIL" if error else "PASS",
                "error": error,
                "assertions": len(self.checks),
                "failed": sum(c["status"] == "FAIL" for c in self.checks),
                "command": self.command,
                "environment": {
                    "python": sys.version,
                    "postgres": subprocess.check_output(
                        ["postgres", "--version"], text=True
                    ).strip(),
                },
                "git_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True
                ).strip(),
                "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
                "code_sha256": code,
                "files": files,
                "work": str(self.work),
                "scope": self.scope,
                "unverified": [
                    "Production migration",
                    "Full-site coverage",
                    "Power-loss durability",
                ],
            },
        )

    def close(self):
        for process, log in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
            log.close()
        self.site.close()
        if self.started:
            subprocess.run(
                ["pg_ctl", "-D", str(self.pg), "-m", "immediate", "-w", "stop"],
                capture_output=True,
                check=True,
            )
        self.temp.cleanup()
