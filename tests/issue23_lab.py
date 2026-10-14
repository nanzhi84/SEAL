"""Isolated Issue23 black-box CLI/HTTP/SQL/S3 lab; no product implementation import."""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
import uuid

from cryptography.fernet import Fernet
from issue20_fixture import Origin
from issue22_lab import ROOT, digest, write
from issue22_lab import Lab as Infrastructure


class Lab(Infrastructure):
    def __init__(self):
        self.name = "issue23-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.work, self.artifacts = (ROOT / x / self.name for x in ("work", "artifacts"))
        self.work.mkdir(parents=True)
        self.artifacts.mkdir(parents=True)
        self.secrets = [secrets.token_hex(32) for _ in range(7)]
        self.token_key = Fernet.generate_key().decode()
        self.secrets.append(self.token_key)
        self.keys = [secrets.token_urlsafe(40) for _ in range(3)]
        self.secrets.extend(self.keys)
        self.containers, self.checks, self.commands, self.http_receipts = [], [], [], []
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SEAL_")}
        self.bucket = "issue23-archive"
        self.origin = Origin(self.work)
        self.service = None
        self.service_number = 0
        self.snapshots = {}
        self.unverified = ["Production deployment and public source availability",
                           "Real wallclock full token TTL; authentic expired tokens are tested"]
        # Frozen preceding release keeps migration acceptance meaningful after this PR lands.
        self.legacy_ref = "994c8adfebbe9facdd3e74a964f89d32e91fe74d"

    def start(self):
        super().start()
        environment = json.loads((self.artifacts / "environment.json").read_text())
        support = ["issue20_fixture.py", "issue20_lab.py", "issue22_lab.py", "issue22_data.py", "issue22_contract.py"]
        test_paths = sorted((ROOT / "tests").glob("issue23*")) + [ROOT / "tests" / name for name in support]
        environment.update(command="uv run --no-sync python tests/issue23_e2e.py",
                           issue="https://github.com/nanzhi84/SEAL/issues/23",
                           implementation_read=False,
                           test_sha256={p.name: digest(p.read_bytes()) for p in test_paths},
                           migration_sha256={str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sorted((ROOT / "src/seal/migrations").glob("*.sql"))},
                           legacy_ref=self.legacy_ref)
        write(self.artifacts / "environment.json", environment)
        (self.artifacts / "public-contract.md").unlink(missing_ok=True)
        (self.artifacts / "failure-boundaries.json").write_bytes(
            (ROOT / "tests/issue23_failure_boundaries.json").read_bytes())

    def json_cli(self, *args, expected=(0,)):
        result = self.cli(*args, expected=expected)
        try:
            return json.loads(result.stdout)
        except ValueError:
            self.check("CLI JSON output " + " ".join(map(str, args[:2])), False,
                       stdout=self.clean(result.stdout), stderr=self.clean(result.stderr))
            return {}

    def create(self, label):
        objects = self.object_inventory()
        payload = self.json_cli("snapshot", "create")
        ident = payload.get("snapshot_id", payload.get("id"))
        self.check("snapshot create returns ID " + label, isinstance(ident, str) and bool(ident), output=payload)
        if not ident:
            raise RuntimeError("Snapshot create did not return an ID")
        self.snapshots[label] = ident
        self.check("snapshot create does not copy S3 " + label, self.object_inventory() == objects)
        return ident

    def publish(self, ident, expected=(0,)):
        before = self.object_inventory()
        result = self.cli("snapshot", "publish", ident, expected=expected)
        self.check("publication does not copy S3", self.object_inventory() == before)
        return result

    def pointer(self):
        return self.sql("SELECT published_snapshot_id FROM seal.publication WHERE singleton")[0]["published_snapshot_id"]

    def mapping(self, ident):
        sources = self.sql("SELECT * FROM seal.snapshot_source WHERE snapshot_id=%s ORDER BY source_id", (ident,))
        items = self.sql("SELECT i.*,v.result_ref,v.sequence FROM seal.snapshot_item i "
                         "JOIN seal.version v ON v.id=i.version_id WHERE i.snapshot_id=%s "
                         'ORDER BY i.source_id,i.resource_id COLLATE "C"', (ident,))
        return dict(sources=sources, items=items)

    def object_inventory(self):
        return sorted((obj["Key"], obj["Size"], obj["ETag"]) for page in self.s3.get_paginator(
            "list_objects_v2").paginate(Bucket=self.bucket) for obj in page.get("Contents", []))

    def audit(self):
        return self.sql("SELECT * FROM seal.snapshot_publication ORDER BY sequence")

    def write_config(self, callers):
        path = self.work / f"api-{self.service_number}.private.json"
        self.service_config["callers"] = callers
        temporary = path.with_suffix(".tmp")
        write(temporary, self.service_config)
        temporary.chmod(0o600)
        temporary.replace(path)

    def close(self):
        self.stop_service()
        if self.origin.thread.is_alive():
            self.origin.close()
        for name in reversed(self.containers):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        write(self.artifacts / "commands.json", self.commands)
        write(self.artifacts / "snapshots.json", self.snapshots)
        print("ARTIFACTS", self.artifacts, flush=True)

    def summary(self):
        content = "\n".join(p.read_text(errors="replace") for p in self.artifacts.rglob("*") if p.is_file())
        manifest = json.loads((self.artifacts / "environment.json").read_text())
        mismatches = {}
        for field, prefix in (("source_sha256", ROOT), ("migration_sha256", ROOT), ("test_sha256", ROOT / "tests")):
            mismatches[field] = [name for name, expected in manifest[field].items()
                                 if not (prefix / name).is_file() or digest((prefix / name).read_bytes()) != expected]
        self.check("final current implementation/migration/test hashes equal executed manifest", not any(mismatches.values()), mismatches=mismatches)
        remaining = self.docker("ps", "-a", "--filter", "name=" + self.name, "--format", "{{.Names}}").splitlines()
        self.check("all acceptance-created PostgreSQL and S3 containers cleaned", not remaining, remaining=remaining)
        leaked = [secret for secret in self.secrets if secret in content]
        encrypted = re.findall(r"gAAAA[A-Za-z0-9_=-]{20,}", content)
        self.check("acceptance artifacts omit private random credentials and keys",
                   not leaked and not encrypted)
        write(self.artifacts / "audit.json", dict(source_migration_test_hashes_match=not any(mismatches.values()),
              mismatches=mismatches, private_credential_match_count=len(leaked), opaque_token_match_count=len(encrypted),
              all_containers_cleaned=not remaining, remaining_containers=remaining, cleaned_containers=self.containers,
              source_file_count=len(manifest["source_sha256"]), migration_count=len(manifest["migration_sha256"]),
              test_file_count=len(manifest["test_sha256"]), root_minio_accessed=False))
        result = dict(passed=sum(c["status"] == "PASS" for c in self.checks),
                      failed=sum(c["status"] == "FAIL" for c in self.checks),
                      command="uv run --no-sync python tests/issue23_e2e.py",
                      artifacts=str(self.artifacts), work=str(self.work), unverified=self.unverified)
        write(self.artifacts / "summary.json", result)
        return result
