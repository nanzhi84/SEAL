"""Small adapters around the existing acceptance Harness, not a second runtime."""

import hashlib
import json
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

PACK = Path("experiments/seal-v1.1-runtime-golden-fixtures")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


class Smoke:
    def __init__(self, h):
        self.h = h
        self.cases = []
        self.server = None
        self.current = None

    @contextmanager
    def case(self, identity, name):
        start = time.monotonic()
        first = len(self.h.assertions)
        row = {"id": identity, "name": name, "status": "UNVERIFIED"}
        self.current = row
        try:
            yield row
            assertions = self.h.assertions[first:]
            row["status"] = (
                "PASS"
                if assertions and all(a["status"] == "PASS" for a in assertions)
                else "FAIL"
                if assertions
                else "UNVERIFIED"
            )
        except Exception as exc:
            row.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - start, 3)
            row["assertions"] = self.h.assertions[first:]
            self.cases.append(row)
            write(self.h.output / "cases.json", self.cases)
            print(f"{identity}: {row['status']}", flush=True)

    def start_server(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.log = (self.h.output / "fixture-server.log").open("w")
        self.server = subprocess.Popen(
            [sys.executable, str(PACK / "server.py"), "--port", str(port)],
            stdout=self.log,
            stderr=self.log,
        )
        for _ in range(60):
            try:
                self.control("reset")
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("fixture_server_not_ready")

    def control(self, action):
        with urlopen(self.url + "/__control/" + action, timeout=3) as response:
            return json.load(response)

    def stop_server(self):
        if self.server and self.server.poll() is None:
            write(self.h.output / "fixture-request-counts.json", self.control("stats"))
            self.server.terminate()
            self.server.wait(timeout=5)
            self.log.close()

    def bind(self, source, paths, recipe, role="detail", robots=False, params=None):
        self.h.config(
            source,
            entries=[self.url + p for p in paths],
            seed_role=role,
            robots=robots,
            concurrency=1,
            delay=0.0,
        )
        return self.h.binding(source, recipe=recipe, params=params)

    def execute(self, source, binding, queue=False):
        args = ["run", source, "--binding", binding]
        if queue:
            args.append("--enqueue")
        # Technical negative outcomes are evidence, not CLI infrastructure exceptions.
        p = subprocess.run(
            [sys.executable, "-m", "seal", *args],
            env=self.h.env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        data = json.loads(p.stdout)
        self.h.receipts.append({"args": args, "exit": p.returncode, "result": data})
        if "run_id" not in data:
            raise AssertionError(data)
        if queue:
            self.h.start_worker()
            self.h.wait_worker()
        run = self.h.cli("inspect", "run", data["run_id"])
        exported = self.h.cli("export", source, "--run", data["run_id"])
        if self.current is not None:
            self.current.setdefault("runs", []).append(
                {
                    "run_id": data["run_id"],
                    "binding": binding,
                    "queue": queue,
                    "status": run["run"]["status"],
                    "errors": run["run"]["errors"],
                    "documents": exported["documents"],
                }
            )
        return run, exported

    def object(self, key):
        path = self.h.root / "archive" / "objects" / key[:2] / key[2:]
        data = path.read_bytes()
        self.h.check("object_sha256:" + key, sha(data), key)
        return data

    def lineage(self, source, run, exported):
        h = self.h
        state = h.cli("inspect", "source", source)
        revisions = {r["id"]: r for r in state["revisions"]}
        results = {r["id"]: r for r in run["results"]}
        inputs = run["run"]["inputs"]
        h.check("quality_not_evaluated", exported["quality_status"], "not_evaluated")
        h.check("export_run_status", exported["run"]["status"], run["run"]["status"])
        h.check(
            "report_not_evaluated", exported["run"]["report"]["quality_status"], "not_evaluated"
        )
        for doc in exported["documents"]:
            h.check("document_execution_status", doc["run_status"], run["run"]["status"])
            h.check("document_not_evaluated", doc["quality_status"], "not_evaluated")
            result = results[doc["result_id"]]
            revision = revisions[doc["revision_id"]]
            binding = h.cli("inspect", "binding", doc["binding_id"])
            h.check(
                "export_run_binding_version",
                [doc["run_id"], result["binding_id"], doc["recipe_version"], binding["source_id"]],
                [run["run"]["id"], doc["binding_id"], binding["recipe_version"], source],
            )
            h.check(
                "output_link_frozen",
                {
                    "result_id": doc["result_id"],
                    "revision_id": doc["revision_id"],
                    "observation_id": doc["observation_id"],
                }
                in run["run"]["report"]["outputs"],
            )
            h.check("document_revision_result", revision["document_id"], result["document_id"])
            h.check(
                "input_observation_link",
                any(
                    i["observation_id"] == doc["observation_id"]
                    and i["snapshot_id"] in result["inputs"]
                    for i in inputs
                ),
            )
            for key in doc["snapshot_ids"]:
                snapshot = json.loads(self.object(key))
                self.object(snapshot["body_hash"])
                h.check(
                    "input_url_body",
                    [snapshot["url"], snapshot["body_hash"]],
                    [doc["url"], doc["body_hash"]],
                )
        return state

    def changed_recipe(self, recipe_path):
        target = self.h.root / "changed-recipe"
        shutil.copytree(recipe_path, target)
        file = target / "recipe.py"
        file.write_text(
            file.read_text().replace(
                "TITLE_SELECTOR = \"h1.title, h2[data-field='title']\"",
                "TITLE_SELECTOR = \"main#notice h1.title, article header h2[data-field='title']\"",
            )
        )
        return self.h.cli("recipe", "pack", target)["recipe_version"]


@contextmanager
def offline_guard(h):
    """Audit every Python TCP connect; only the disposable PostgreSQL port is allowed."""
    folder = h.root / "network-guard"
    folder.mkdir(exist_ok=True)
    log = h.output / "offline-network-audit.jsonl"
    (folder / "sitecustomize.py").write_text("""import json, os, sys
log = os.environ["SMOKE_NETWORK_AUDIT"]
port = int(os.environ["SMOKE_DB_PORT"])
def emit(value):
    with open(log, "a") as file: file.write(json.dumps(value) + "\\n")
emit({"event": "guard_installed", "pid": os.getpid()})
def audit(event, args):
    if event == "socket.connect":
        address = args[1]
        allowed = isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1") and address[1] == port
        if not allowed:
            emit({"event": "blocked_connect", "address": str(address), "pid": os.getpid()})
            raise OSError("smoke_offline_network_forbidden")
sys.addaudithook(audit)
""")
    keys = ["PYTHONPATH", "SMOKE_NETWORK_AUDIT", "SMOKE_DB_PORT"]
    before = {k: h.env.get(k) for k in keys}
    h.env.update(PYTHONPATH=str(folder), SMOKE_NETWORK_AUDIT=str(log), SMOKE_DB_PORT=str(h.port))
    try:
        # Positive control: the guard must detect an actual attempted TCP connection.
        probe = subprocess.run(
            [sys.executable, "-c", "import socket; socket.create_connection(('127.0.0.1', 9))"],
            env=h.env,
            capture_output=True,
        )
        h.check("offline_guard_positive_control", probe.returncode != 0)
        offset = len(log.read_text().splitlines())
        yield
        entries = [json.loads(line) for line in log.read_text().splitlines()[offset:]]
        h.check("offline_guard_reached_subprocesses", len(entries) >= 2)
        h.check(
            "offline_no_network_attempt",
            [e for e in entries if e["event"] == "blocked_connect"],
            [],
        )
    finally:
        for key, value in before.items():
            if value is None:
                h.env.pop(key, None)
            else:
                h.env[key] = value
