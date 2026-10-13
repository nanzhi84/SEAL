"""Issue #17 黑盒支持：独立 PostgreSQL、CLI 进程及可审计断言。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import pty
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def json_write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


class Lab:
    def __init__(self, root: Path, name: str | None = None):
        self.root = root
        self.name = name or "issue17-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:5]
        self.work = root / "work" / self.name
        self.artifacts = root / "artifacts" / self.name
        self.work.mkdir(parents=True, exist_ok=False)
        self.artifacts.mkdir(parents=True, exist_ok=False)
        self.container = "seal-" + self.name
        self.command_number = 0
        self.assertions: list[dict] = []
        self.commands: list[dict] = []
        self.processes: list[subprocess.Popen] = []
        self.tty_readers: dict[int, tuple[threading.Thread, list[bytes]]] = {}
        self.database_url = ""
        self.archive = self.work / "archive"
        self.archive.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("SEAL_")}
        self.script_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (root / "tests").glob("issue17_*.py")
        }

    def start_database(self):
        result = subprocess.run(
            ["docker", "run", "--detach", "--name", self.container,
             "--publish", "127.0.0.1::5432", "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
             "--env", "POSTGRES_DB=seal_e2e", "--volume", f"{self.work / 'postgres'}:/var/lib/postgresql/data",
             "postgres:17-alpine"], text=True, capture_output=True, check=True,
        )
        json_write(self.artifacts / "database-environment.json", {
            "image": "postgres:17-alpine", "container": self.container,
            "id": result.stdout.strip(), "loopback_only": True, "synthetic_data_only": True,
        })
        port = subprocess.check_output(["docker", "port", self.container, "5432"], text=True).strip().split(":")[-1]
        self.database_url = f"postgresql://postgres@127.0.0.1:{port}/seal_e2e"
        deadline = time.monotonic() + 40
        while True:
            try:
                with psycopg.connect(self.database_url):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        self.env.update({
            "SEAL_DATABASE_URL": self.database_url, "SEAL_ARCHIVE": str(self.archive),
            "SEAL_ALLOW_LOOPBACK": "1", "NO_COLOR": "1",
        })
        json_write(self.artifacts / "environment.json", {
            "python": sys.version, "executable": sys.executable,
            "postgres": self.sql("SELECT version() AS version")[0]["version"],
            "crawl4ai": importlib.metadata.version("crawl4ai"),
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip(),
            "uv_lock_sha256": hashlib.sha256((self.root / "uv.lock").read_bytes()).hexdigest(),
            "command": "uv run --no-sync python tests/issue17_e2e.py",
            "data_preconditions": "No credentials. Synthetic HTTP/PDF fixture. Isolated trust-auth PostgreSQL bound to loopback.",
        })

    def sql(self, query: str, params=(), *, url: str | None = None):
        with psycopg.connect(url or self.database_url, row_factory=dict_row, autocommit=True) as connection:
            cursor = connection.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def check(self, label: str, condition: bool, **evidence):
        self.assertions.append({"label": label, "status": "PASS" if condition else "FAIL", **evidence})
        if not condition or not label.startswith("Observation hash recomputed"):
            print(f"{'PASS' if condition else 'FAIL'} {label}", flush=True)
        json_write(self.artifacts / "assertions.json", self.assertions)

    def unknown(self, label: str, reason: str):
        self.assertions.append({"label": label, "status": "UNVERIFIED", "reason": reason})
        print(f"UNVERIFIED {label}: {reason}", flush=True)

    def command(self, *args: str, timeout=120, expected=0, env=None, executable=None):
        self.command_number += 1
        prefix = f"{self.command_number:03d}-" + "-".join(args[:1])
        cmd = (executable or ["uv", "run", "--no-sync", "seal"]) + list(args)
        if args[0] in ("collect", "recheck") and len(args) > 1:
            source = Path(args[1])
            snapshot = self.artifacts / "config" / prefix
            snapshot.mkdir(parents=True, exist_ok=True)
            (snapshot / source.name).write_bytes(source.read_bytes())
        start = time.monotonic()
        completed = subprocess.run(cmd, cwd=self.root, env=env or self.env,
                                   capture_output=True, text=True, timeout=timeout)
        self.store_command(prefix, cmd, completed.returncode, completed.stdout, completed.stderr,
                           elapsed=time.monotonic() - start)
        receipt = None
        try:
            receipt = json.loads(completed.stdout)
            single_json = isinstance(receipt, dict)
        except json.JSONDecodeError:
            single_json = False
        self.check(prefix + " stdout single JSON", single_json, exit_code=completed.returncode)
        if expected is not None:
            accepted = completed.returncode in expected if isinstance(expected, tuple) else completed.returncode == expected
            self.check(prefix + " exit contract", accepted, actual=completed.returncode, expected=expected)
        return completed, receipt

    def store_command(self, prefix, cmd, exit_code, stdout, stderr, **extra):
        (self.artifacts / (prefix + ".stdout.txt")).write_text(stdout)
        (self.artifacts / (prefix + ".stderr.txt")).write_text(stderr)
        record = {"name": prefix, "argv": cmd, "exit_code": exit_code, **extra}
        self.commands.append(record)
        json_write(self.artifacts / "commands.json", self.commands)
        try:
            json_write(self.artifacts / (prefix + ".json"), json.loads(stdout))
        except json.JSONDecodeError:
            pass

    def start_command(self, *args: str, tty=False):
        self.command_number += 1
        prefix = f"{self.command_number:03d}-" + args[0]
        cmd = ["uv", "run", "--no-sync", "seal", *args]
        tty_fd = None
        if tty:
            tty_fd, slave = pty.openpty()
            terminal_environment = {**self.env, "TERM": "xterm-256color"}
            process = subprocess.Popen(cmd, cwd=self.root, env=terminal_environment, stdout=subprocess.PIPE,
                                       stderr=slave, stdin=subprocess.DEVNULL, text=True,
                                       start_new_session=True)
            os.close(slave)
            chunks: list[bytes] = []

            def drain_terminal():
                while True:
                    if not select.select([tty_fd], [], [], 0.1)[0]:
                        continue
                    try:
                        chunk = os.read(tty_fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)

            reader = threading.Thread(target=drain_terminal, daemon=True)
            reader.start()
            self.tty_readers[process.pid] = (reader, chunks)
        else:
            process = subprocess.Popen(cmd, cwd=self.root, env=self.env, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(process)
        return process, prefix, cmd, tty_fd

    def finish_command(self, handle, *, timeout=100):
        process, prefix, cmd, tty_fd = handle
        stdout, stderr = process.communicate(timeout=timeout)
        if tty_fd is not None:
            reader, chunks = self.tty_readers.pop(process.pid)
            reader.join(timeout=2)
            os.close(tty_fd)
            stderr = b"".join(chunks).decode(errors="replace")
        self.store_command(prefix, cmd, process.returncode, stdout, stderr or "")
        return process.returncode, stdout, stderr or ""

    def snapshot(self, name: str):
        tables = self.sql("SELECT table_name FROM information_schema.tables WHERE table_schema='seal' AND table_type='BASE TABLE' ORDER BY table_name")
        output = {}
        for row in tables:
            table = row["table_name"]
            records = self.sql(f'SELECT row_to_json(t) AS row FROM seal."{table}" t')
            output[table] = sorted([entry["row"] for entry in records], key=lambda entry: json.dumps(entry, sort_keys=True, default=str))
        json_write(self.artifacts / (name + ".database.json"), output)
        return output

    def archive_hashes(self):
        return {str(path.relative_to(self.archive)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.archive.rglob("*") if path.is_file()}

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        subprocess.run(["docker", "stop", "--time", "2", self.container], capture_output=True)
        subprocess.run(["docker", "rm", self.container], capture_output=True)

    def report(self):
        request_log = self.work / "fixture" / "requests.jsonl"
        if request_log.exists():
            (self.artifacts / "fixture-requests.jsonl").write_bytes(request_log.read_bytes())
        json_write(self.artifacts / "acceptance-code-hashes.json", self.script_hashes)
        statuses = [item["status"] for item in self.assertions]
        result = {
            "run": self.name, "passed": statuses.count("PASS"), "failed": statuses.count("FAIL"),
            "unverified": statuses.count("UNVERIFIED"), "assertions": self.assertions,
            "commands": self.commands, "work": str(self.work), "artifacts": str(self.artifacts),
            "scope": "Synthetic loopback HTML/PDF + isolated PostgreSQL; no production migration or whole-site completeness claim",
        }
        json_write(self.artifacts / "summary.json", result)
        return result
