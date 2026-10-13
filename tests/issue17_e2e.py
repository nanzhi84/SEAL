"""Issue #17 完全独立业务验收；不导入 SEAL 或阅读其实现。

先于执行冻结的失败方式：跨来源合并、query 丢失、正文/hash 不一致、临时文件失效、
headers/config 冒充变化、A→B→A 去重丢历史、解析失败后不重试、403/429/登录/挑战
推进当前指针、长期 visited 排除新增链接、导航移除导致已知资源消失、重复提交新增
版本、checkpoint 丢当前层、取消不落状态、旧会话接管后覆盖新结果、旧表/迁移被破坏。

命令：uv run python tests/issue17_e2e.py
前提：Docker（postgres:17-alpine）、uv 已同步依赖及 Crawl4AI SDK、本机 loopback 可用。
输入：tests/issue17_fixture.py 生成真实 HTTP/PDF；dd-001/dd-002 仅模拟既有来源，
无公网请求、无生产数据。输出：work/issue17-*、artifacts/issue17-*；退出 0 仅代表
有界 oracle 全部通过，不证明全站完整、Office/OCR 或生产迁移。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

import yaml
from issue17_fixture import Fixture, html_bytes
from issue17_migration_e2e import LEGACY_REF, run_migration
from issue17_rendered_e2e import run_rendered
from issue17_support import Lab, json_write

PATH_ONE = "/article?edition=1"


class Acceptance:
    def __init__(self, lab: Lab, fixture: Fixture):
        self.lab = lab
        self.fixture = fixture
        self.configs = {}
        for source_id, entry in (("dd-001", "/a/"), ("dd-002", "/b/")):
            config = {
                "id": source_id, "entry_urls": [fixture.base + entry],
                "allowed_hosts": ["127.0.0.1"], "archive_approved": True,
                "allowed_path_prefixes": ["/a/", "/b/", "/article", "/gone", "/file.pdf",
                                          "/added", "/retry", "/slow", "/robots-denied", "/after-crash"],
                "scope": "Isolated synthetic business acceptance; not a registered public source",
                "robots": True, "budget": {"requests": 100, "seconds": 70, "response_bytes": 1048576},
                "concurrency": 1, "recheck_seconds": 0,
                "crawl4ai": {"mode": "http", "max_depth": 3, "page_timeout_ms": 30000,
                             "word_count_threshold": 0},
            }
            path = lab.work / (source_id + ".yaml")
            path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
            self.configs[source_id] = path
        self.config = self.configs["dd-001"]
        self.initial_versions = []

    def collect(self, source_id="dd-001", *, expected=0):
        return self.lab.command("collect", str(self.configs[source_id]), "--no-progress", expected=expected)

    def recheck(self, source_id="dd-001", *, expected=0):
        return self.lab.command("recheck", str(self.configs[source_id]), "--no-progress", expected=expected)

    def resource(self, path=PATH_ONE, source_id="dd-001"):
        rows = self.lab.sql("SELECT r.* FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id WHERE f.source_id=%s AND r.url=%s", (source_id, self.fixture.base + path))
        if len(rows) != 1:
            raise AssertionError(f"Expected exactly one Resource: {source_id} {path}; got {len(rows)}")
        return rows[0]

    def versions(self, path=PATH_ONE, source_id="dd-001"):
        resource = self.resource(path, source_id)
        return self.lab.sql("SELECT * FROM seal.version WHERE resource_id=%s ORDER BY sequence", (resource["id"],))

    def observations(self, path=PATH_ONE, source_id="dd-001"):
        resource = self.resource(path, source_id)
        return self.lab.sql("SELECT * FROM seal.observation WHERE resource_id=%s ORDER BY observed_at,id", (resource["id"],))

    def count(self, count, *, path=PATH_ONE, source_id="dd-001", label="version count"):
        versions = self.versions(path, source_id)
        self.lab.check(label, len(versions) == count, expected=count, actual=len(versions), source_id=source_id, path=path)
        self.lab.check(label + " contiguous sequences", [v["sequence"] for v in versions] == list(range(1, len(versions) + 1)))
        if versions:
            self.lab.check(label + " current points at last immutable Version", str(self.resource(path, source_id)["current_version_id"]) == str(versions[-1]["id"]))
        return versions

    def initial(self):
        self.lab.command("init")
        for command, options in (("init", ("--backup", "--acknowledge-data-loss")),
                                 ("collect", ("--enqueue", "--no-progress")),
                                 ("recheck", ("--enqueue", "--no-progress")),
                                 ("worker", ("--once",))):
            result = subprocess.run(["uv", "run", "--no-sync", "seal", command, "--help"], cwd=self.lab.root, env=self.lab.env, capture_output=True, text=True)
            self.lab.check(command + " retained control options", result.returncode == 0 and all(option in result.stdout for option in options))
            (self.lab.artifacts / (command + ".help.txt")).write_text(result.stdout)
        self.fixture.root_links["/a/"] += ("/outside", "/robots-denied")
        _, blocked_receipt = self.collect(expected=1)
        self.lab.check("policy-blocked run does not claim complete site coverage", blocked_receipt.get("report", {}).get("scope_evidence", {}).get("unknown_coverage") is True)
        self.collect("dd-002")
        frontiers = self.lab.sql("SELECT source_id,count(*) AS n FROM seal.frontier GROUP BY source_id ORDER BY source_id")
        self.lab.check("Source 1:1 Frontier", frontiers == [{"source_id": "dd-001", "n": 1}, {"source_id": "dd-002", "n": 1}], rows=frontiers)
        for source_id in self.configs:
            for path in (PATH_ONE, "/article?edition=2", "/gone", "/file.pdf"):
                self.count(1, path=path, source_id=source_id, label="initial " + source_id + " " + path)
        self.lab.check("same URL kept separately under different Source", self.resource()["id"] != self.resource(source_id="dd-002")["id"])
        self.lab.check("semantic query parameters remain distinct", self.resource(PATH_ONE)["id"] != self.resource("/article?edition=2")["id"])
        self.lab.check("scope excludes forbidden path", self.fixture.count("/outside") == 0)
        self.lab.check("robots excludes denied URL", self.fixture.count("/robots-denied") == 0)
        self.fixture.root_links["/a/"] = self.fixture.links
        self.initial_versions = self.lab.sql("SELECT * FROM seal.version ORDER BY cursor")
        self.lab.snapshot("initial")

    def timeline(self):
        before = len(self.observations())
        _, receipt = self.recheck(expected=1)
        self.lab.check("known robots-blocked Resource recheck reports only policy failure", receipt.get("errors") == ["robots_denied"])
        self.count(1, label="unchanged response / volatile header reuse")
        observations = self.observations()
        self.lab.check("unchanged creates Observation linked to current Version", len(observations) > before and str(observations[-1]["version_id"]) == str(self.resource()["current_version_id"]))
        self.recheck("dd-002")
        self.count(1, source_id="dd-002", label="fully allowed Source recheck succeeds without new Version")
        alpha = self.fixture.responses[PATH_ONE][2]
        beta = html_bytes("Article One", "Beta changed source content.")
        self.fixture.set(PATH_ONE, body=beta)
        self.collect()
        self.count(2, label="A to B appends V2")
        self.fixture.set(PATH_ONE, body=alpha)
        self.collect()
        versions = self.count(3, label="B to A appends V3")
        hashes = {str(o["id"]): o["body_hash"] for o in self.observations()}
        version_hashes = [hashes[str(v["observation_id"])] for v in versions]
        self.lab.check("A-B-A response oracle", version_hashes == [hashlib.sha256(alpha).hexdigest(), hashlib.sha256(beta).hexdigest(), hashlib.sha256(alpha).hexdigest()], hashes=version_hashes)
        self.count(1, source_id="dd-002", label="other Source timeline unaffected")
        changed_config = yaml.safe_load(self.config.read_text())
        changed_config["crawl4ai"]["word_count_threshold"] = 1
        self.config.write_text(yaml.safe_dump(changed_config, sort_keys=False))
        self.collect()
        self.count(3, label="parser config change alone is not a content Version")

    def retry_and_errors(self):
        retry_body = html_bytes("Article One", "Retry same source bytes after parser rejection.")
        self.fixture.set(PATH_ONE, mime="application/octet-stream", body=retry_body)
        self.collect(expected=(0, 1))
        self.count(3, label="unsupported changed input does not advance")
        rows = self.observations()
        self.lab.check("parse failure body archived and Version absent", rows[-1]["body_hash"] == hashlib.sha256(retry_body).hexdigest() and rows[-1]["version_id"] is None, observation=rows[-1])
        self.fixture.set(PATH_ONE, body=retry_body)
        self.collect()
        self.count(4, label="same failed bytes retry succeeds")
        for status, name, body in (
            (403, "HTTP 403", b"Forbidden"), (429, "HTTP 429", b"Too many requests"),
            (200, "login 200", html_bytes("Login", '<form><input type="password"><button>Sign in</button></form>')),
            (200, "challenge 200", html_bytes("Security check", "Verify you are human. CAPTCHA challenge.")),
            ("network", "network disconnect", b""),
        ):
            self.fixture.set(PATH_ONE, status=status, body=body)
            self.collect(expected=(0, 1))
            self.count(4, label=name + " does not advance current")
            observation = self.observations()[-1]
            self.lab.check(name + " failed/blocked Observation", observation["version_id"] is None and observation["outcome"] not in ("success", "unchanged", "created"), observation=observation)
            if body:
                self.lab.check(name + " preserves response bytes hash", observation["body_hash"] == hashlib.sha256(body).hexdigest())
        self.fixture.set(PATH_ONE, body=retry_body)

    def new_round_and_orphan(self):
        self.fixture.root_links["/a/"] = (PATH_ONE, "/article?edition=2", "/file.pdf", "/added")
        self.collect()
        self.count(1, path="/added", label="new round revisits entry and discovers new link")
        before = self.fixture.count("/gone")
        self.fixture.set("/gone", body=html_bytes("Orphan", "Orphan changed while not in navigation."))
        _, receipt = self.recheck(expected=1)
        self.lab.check("orphan recheck isolates known robots policy failure", receipt.get("errors") == ["robots_denied"])
        self.count(2, path="/gone", label="known orphan resource rechecked")
        self.lab.check("orphan actual HTTP requested", self.fixture.count("/gone") > before)

    def queue(self):
        before = self.fixture.count(PATH_ONE)
        before_versions = self.versions()
        self.lab.command("collect", str(self.config), "--enqueue", "--no-progress")
        self.lab.check("enqueue performs no HTTP before Worker", self.fixture.count(PATH_ONE) == before)
        self.lab.command("worker", "--once")
        self.lab.check("Worker executes queued collect", self.fixture.count(PATH_ONE) > before)
        self.lab.check("queued unchanged collect no duplicate Version", self.versions() == before_versions)
        self.lab.command("recheck", str(self.config), "--enqueue", "--no-progress")
        self.lab.command("worker", "--once")
        after = self.fixture.count(PATH_ONE)
        self.lab.command("worker", "--once")
        self.lab.check("empty Worker once has no duplicate delivery", self.fixture.count(PATH_ONE) == after)

    def process_contracts(self):
        self.fixture.root_links["/a/"] = ("/slow", "/added", PATH_ONE)
        self.fixture.delay["/slow"] = 6
        before = self.fixture.count("/slow")
        handle = self.lab.start_command("collect", str(self.config), tty=True)
        self.fixture.wait_for("/slow", before + 1)
        time.sleep(0.8)
        os.killpg(handle[0].pid, signal.SIGINT)
        code, stdout, stderr = self.lab.finish_command(handle)
        try:
            receipt = json.loads(stdout)
        except json.JSONDecodeError:
            receipt = {}
        self.lab.check("Ctrl+C exit130 with single JSON receipt", code == 130 and isinstance(receipt, dict) and bool(receipt), exit_code=code)
        self.lab.check("TTY stderr renders progress", bool(stderr.strip()))
        self.fixture.delay["/slow"] = 0
        self.collect()
        self.count(1, path="/slow", label="cancel checkpoint recovery commits pending response")
        self.fixture.set("/slow", body=html_bytes("Slow", "Changed response before forced process crash."))
        self.fixture.set("/added", body=html_bytes("Added", "Pending sibling changed before the process crash."))
        self.fixture.root_links["/a/"] = ("/slow", "/after-crash", "/added", PATH_ONE)
        self.fixture.delay["/slow"] = 8
        before = self.fixture.count("/slow")
        handle = self.lab.start_command("collect", str(self.config), "--no-progress")
        self.fixture.wait_for("/slow", before + 1)
        os.killpg(handle[0].pid, signal.SIGKILL)
        self.lab.finish_command(handle)
        self.lab.snapshot("after-crash")
        self.fixture.delay["/slow"] = 0
        self.collect()
        self.count(2, path="/slow", label="SIGKILL checkpoint recovery does not lose in-flight current layer")
        self.count(2, path="/added", label="SIGKILL recovery does not discard queued sibling change")
        self.count(1, path="/after-crash", label="SIGKILL recovery fetches promised newly discovered sibling")
        duplicates = self.lab.sql("SELECT run_id,url,count(*) AS n FROM seal.observation GROUP BY run_id,url HAVING count(*)>1")
        self.lab.check("checkpoint redelivery observation idempotence per run URL", not duplicates, duplicates=duplicates)

    def concurrency_and_fencing(self):
        self.fixture.delay["/slow"] = 10
        before = self.fixture.count("/slow")
        handle = self.lab.start_command("collect", str(self.config), "--no-progress")
        self.fixture.wait_for("/slow", before + 1)
        os.killpg(handle[0].pid, signal.SIGSTOP)
        competing, _ = self.collect(expected=(1, 2))
        self.lab.check("one active executor per Frontier", competing.returncode != 0)
        self.collect("dd-002")
        self.lab.check("other Source proceeds while executor stopped", self.versions(source_id="dd-002")[-1]["sequence"] >= 1)
        os.killpg(handle[0].pid, signal.SIGCONT)
        self.lab.finish_command(handle)
        old_body = html_bytes("Slow", "Stale executor source representation.")
        new_body = html_bytes("Slow", "New executor source representation.")
        self.fixture.set("/slow", body=old_body)
        before = self.fixture.count("/slow")
        handle = self.lab.start_command("collect", str(self.config), "--no-progress")
        self.fixture.wait_for("/slow", before + 1)
        os.killpg(handle[0].pid, signal.SIGSTOP)
        owners = self.lab.sql("SELECT DISTINCT a.pid FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid WHERE a.datname=current_database() AND l.locktype='advisory' AND l.granted AND a.pid<>pg_backend_pid()")
        self.lab.check("Frontier lease visible as real DB session lock", bool(owners), owners=owners)
        actor_sessions = self.lab.sql("SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()")
        self.lab.check("isolated actor has visible database sessions for outage", bool(actor_sessions), sessions=actor_sessions)
        for actor in actor_sessions:
            self.lab.sql("SELECT pg_terminate_backend(%s,2000)", (actor["pid"],))
        self.fixture.set("/slow", body=new_body)
        self.fixture.delay["/slow"] = 0
        self.collect()
        new_pointer = self.resource("/slow")["current_version_id"]
        owners_versions = self.versions("/slow")
        owners_observations = {str(row["id"]): row for row in self.observations("/slow")}
        self.lab.check("new lease owner commits actual fresh response before stale executor resumes", bool(owners_versions) and owners_observations[str(owners_versions[-1]["observation_id"])]["body_hash"] == hashlib.sha256(new_body).hexdigest())
        os.killpg(handle[0].pid, signal.SIGCONT)
        self.lab.finish_command(handle)
        self.lab.check("lost-session old task cannot replace new current Version", self.resource("/slow")["current_version_id"] == new_pointer)
        hashes = {str(o["id"]): o["body_hash"] for o in self.observations("/slow")}
        old_hash = hashlib.sha256(old_body).hexdigest()
        self.lab.check("stale response cannot create successful Version", all(hashes[str(v["observation_id"])] != old_hash for v in self.versions("/slow")))

    def recover_worker_target(self, run_id, *, timeout=45):
        """公开 --once 只消费当前就绪任务；按目标 Run 有界等待正常延期。"""
        observations = []
        started_at = time.monotonic()
        deadline = started_at + timeout

        def observe():
            run = self.lab.sql("SELECT id,status,attempt_epoch,job_id,deadline FROM seal.run WHERE id=%s", (run_id,))[0]
            queue = self.lab.sql("SELECT id,status,attempts,scheduled_at,worker_id,clock_timestamp() AS observed_at,"
                                 " greatest(0,extract(epoch FROM scheduled_at-clock_timestamp())) AS wait_seconds, "
                                 " status=\'todo\' AND (scheduled_at IS NULL OR scheduled_at<=clock_timestamp()) AS ready_now "
                                 "FROM procrastinate_jobs WHERE id=%s", (run["job_id"],))
            events = self.lab.sql("SELECT id,job_id,type,at FROM procrastinate_events WHERE job_id=%s ORDER BY id", (run["job_id"],))
            observations.append({"run": run, "queue": queue, "events": events})
            json_write(self.lab.artifacts / "worker-cancel-recovery-attempts.json", observations)
            return run, queue

        run, queue = observe()
        original_epoch = run["attempt_epoch"]
        while run["status"] not in ("complete", "failed") and time.monotonic() < deadline:
            delay = float(queue[0]["wait_seconds"] or 0) if queue else 0
            if delay > 0:
                time.sleep(min(delay + 0.2, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            self.lab.command("worker", "--once")
            run, queue = observe()
            if run["status"] not in ("complete", "failed"):
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        elapsed = time.monotonic() - started_at
        self.lab.check("cancelled Worker recovery completes exact queued Run within bound",
                       run["status"] == "complete" and run["attempt_epoch"] > original_epoch and elapsed <= timeout,
                       run_id=run_id, status=run["status"], attempt_epoch=run["attempt_epoch"],
                       timeout_seconds=timeout, elapsed_seconds=elapsed)

    def worker_process_contracts(self):
        initial_slow = len(self.versions("/slow"))
        initial_added = len(self.versions("/added"))
        self.fixture.set("/slow", body=html_bytes("Slow", "Queued Worker must recover this changed response."))
        self.fixture.set("/added", body=html_bytes("Added", "Worker pending sibling changed during crash scenario."))
        self.fixture.delay["/slow"] = 8
        before = self.fixture.count("/slow")
        self.lab.command("collect", str(self.config), "--enqueue", "--no-progress")
        handle = self.lab.start_command("worker", "--once")
        self.fixture.wait_for("/slow", before + 1)
        os.killpg(handle[0].pid, signal.SIGKILL)
        self.lab.finish_command(handle)
        self.fixture.delay["/slow"] = 0
        killed_at = time.time()
        print("WAIT Worker public 30s heartbeat recovery window: 31s", flush=True)
        time.sleep(31)
        json_write(self.lab.artifacts / "worker-recovery-timing.json", {
            "heartbeat_expiry_seconds": 30, "worker_killed_at": killed_at,
            "recovery_attempt_at": time.time(), "actual_wait_seconds": time.time() - killed_at,
            "database_heartbeat_mutated": False,
        })
        self.lab.command("worker", "--once")
        self.count(initial_slow + 1, path="/slow", label="Worker SIGKILL queued delivery recovery")
        self.count(initial_added + 1, path="/added", label="Worker SIGKILL checkpoint retains pending sibling")
        before = self.fixture.count("/slow")
        self.lab.command("worker", "--once")
        self.lab.check("recovered Worker does not redeliver completed job", self.fixture.count("/slow") == before)
        current = self.resource("/slow")["current_version_id"]
        accepted_versions = len(self.versions("/slow"))
        self.fixture.set("/slow", body=html_bytes("Slow", "Interrupted active continuous Worker response must recover."))
        self.fixture.delay["/slow"] = 6
        before = self.fixture.count("/slow")
        _, queued_receipt = self.lab.command("collect", str(self.config), "--enqueue", "--no-progress")
        handle = self.lab.start_command("worker")
        self.fixture.wait_for("/slow", before + 1)
        time.sleep(0.5)
        os.killpg(handle[0].pid, signal.SIGINT)
        code, stdout, _ = self.lab.finish_command(handle)
        try:
            receipt = json.loads(stdout)
        except json.JSONDecodeError:
            receipt = None
        self.lab.check("continuous Worker Ctrl+C emits single JSON and exit130", code == 130 and isinstance(receipt, dict), exit_code=code)
        self.lab.check("active Worker cancellation does not commit unfinished response", self.resource("/slow")["current_version_id"] == current)
        rows = self.lab.sql("SELECT status FROM seal.run WHERE id=%s", (queued_receipt["run_id"],))
        self.lab.check("cancelled active Worker leaves resumable Run", len(rows) == 1 and rows[0]["status"] == "retryable", rows=rows)
        self.fixture.delay["/slow"] = 0
        print("WAIT cancelled Worker public recovery window: 31s", flush=True)
        cancelled_at = time.time()
        time.sleep(31)
        json_write(self.lab.artifacts / "worker-cancel-recovery-timing.json", {
            "heartbeat_expiry_seconds": 30, "worker_cancelled_at": cancelled_at,
            "recovery_attempt_at": time.time(), "actual_wait_seconds": time.time() - cancelled_at,
            "database_heartbeat_mutated": False,
        })
        self.recover_worker_target(queued_receipt["run_id"])
        self.count(accepted_versions + 1, path="/slow", label="active continuous Worker Ctrl+C checkpoint recovery")

    def persistency(self):
        output = self.lab.snapshot("final")
        archive_hashes = self.lab.archive_hashes()
        json_write(self.lab.artifacts / "archive-hashes.json", archive_hashes)
        for observation in output.get("observation", []):
            body_hash = observation.get("body_hash")
            if body_hash:
                archived = self.lab.archive / "objects" / body_hash[:2] / body_hash[2:]
                self.lab.check("Observation hash recomputed from durable archive " + str(observation["id"]), observation.get("archive_ref") == body_hash and archived.is_file() and hashlib.sha256(archived.read_bytes()).hexdigest() == body_hash)
        pdf_hash = hashlib.sha256(self.fixture.responses["/file.pdf"][2]).hexdigest()
        self.lab.check("PDF archive is file body rather than temporary download path", pdf_hash in archive_hashes.values())
        all_versions = self.lab.sql("SELECT * FROM seal.version ORDER BY cursor")
        installed_framework = importlib.metadata.version("crawl4ai")
        self.lab.check("accepted Versions retain frozen framework version", all(installed_framework in row["framework_version"] for row in all_versions))
        self.lab.check("accepted Versions retain parser configuration snapshot", all(isinstance(row["parser_config"], dict) and row["parser_config"] for row in all_versions))
        durable_artifacts = True
        for row in all_versions:
            envelope = row["crawl_result"]
            references = list(envelope.get("artifacts", {}).values())
            references += envelope.get("result", {}).get("downloaded_files") or []
            for reference in references:
                if not isinstance(reference, str) or len(reference) != 64:
                    durable_artifacts = False
                    continue
                external = self.lab.archive / "objects" / reference[:2] / reference[2:]
                durable_artifacts &= external.is_file() and hashlib.sha256(external.read_bytes()).hexdigest() == reference
        self.lab.check("CrawlResult external and downloaded files retain readable CAS references", durable_artifacts)
        json_write(self.lab.artifacts / "crawl-results.json", [row["crawl_result"] for row in all_versions])
        script = "import json,sys; from crawl4ai import CrawlResult; rows=json.load(open(sys.argv[1])); results=[CrawlResult.model_validate(row['result']) for row in rows]; assert results; assert all(r.url for r in results); print(json.dumps({'readable_results':len(results)}))"
        process = subprocess.run(["uv", "run", "--no-sync", "python", "-c", script, str(self.lab.artifacts / "crawl-results.json")], cwd=self.lab.root, capture_output=True, text=True)
        (self.lab.artifacts / "cross-process-crawl-result.stdout.txt").write_text(process.stdout)
        (self.lab.artifacts / "cross-process-crawl-result.stderr.txt").write_text(process.stderr)
        self.lab.check("public CrawlResult durable cross-process deserialization", process.returncode == 0, output=process.stdout, exit_code=process.returncode)
        by_id = {str(row["id"]): row for row in all_versions}
        self.lab.check("all original Versions remain immutable", all(by_id.get(str(row["id"])) == row for row in self.initial_versions))
        cursors = [row["cursor"] for row in all_versions]
        self.lab.check("downstream increment cursors stable and strictly ordered", all(a < b for a, b in zip(cursors, cursors[1:], strict=False)) and len(set(cursors)) == len(cursors))
        pdf_results = [row["crawl_result"] for row in self.versions("/file.pdf")]
        self.lab.check("PDF parsed output contains original PDF text", any("SEAL PDF original sample" in json.dumps(row) for row in pdf_results))
        for obsolete in ("document", "document_version", "document_reference", "document_emission", "raw_revision"):
            if obsolete in output:
                self.lab.check("no old content path new writes " + obsolete, not output[obsolete])
        try:
            importlib.metadata.version("Scrapy")
            scrapy_present = True
        except importlib.metadata.PackageNotFoundError:
            scrapy_present = False
        self.lab.check("execution environment has no Scrapy dependency", not scrapy_present)
        for source_id, config_path in self.configs.items():
            (self.lab.artifacts / (source_id + ".yaml")).write_text(config_path.read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name")
    parser.add_argument("--legacy-ref", default=LEGACY_REF, help="冻结旧CLI fixture ref（不随HEAD变化）")
    parser.add_argument("--skip-process", action="store_true", help="仅诊断；跳过时报告 UNVERIFIED")
    parser.add_argument("--skip-migration", action="store_true", help="仅诊断；跳过时报告 UNVERIFIED")
    parser.add_argument("--skip-rendered", action="store_true", help="仅诊断；跳过时报告 UNVERIFIED")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    lab = Lab(root, arguments.name)
    fixture = Fixture(lab.work / "fixture")
    json_write(lab.artifacts / "oracle.json", {"issue": "https://github.com/nanzhi84/SEAL/issues/17", "failure_modes": __doc__, "fixture_only": True})
    try:
        lab.start_database()
        fixture.start()
        acceptance = Acceptance(lab, fixture)
        phases = [acceptance.initial, acceptance.timeline, acceptance.retry_and_errors,
                  acceptance.new_round_and_orphan, acceptance.queue]
        if not arguments.skip_process:
            phases += [acceptance.process_contracts, acceptance.concurrency_and_fencing,
                       acceptance.worker_process_contracts]
        else:
            lab.unknown("real process cancellation/crash/concurrency/fencing", "--skip-process used for diagnostic run")
        for phase in phases:
            print("PHASE " + phase.__name__, flush=True)
            try:
                phase()
            except Exception as error:
                lab.check("phase " + phase.__name__, False, error=str(error), traceback=traceback.format_exc())
        if arguments.skip_rendered:
            lab.unknown("real rendered DOM and browser network scope", "--skip-rendered used for diagnostic run")
        else:
            try:
                run_rendered(acceptance)
            except Exception as error:
                lab.check("rendered DOM oracle", False, error=str(error), traceback=traceback.format_exc())
        try:
            acceptance.persistency()
        except Exception as error:
            lab.check("durability oracle", False, error=str(error), traceback=traceback.format_exc())
        if arguments.skip_migration:
            lab.unknown("historical migration and actual backup restore", "--skip-migration used for diagnostic run")
        else:
            try:
                run_migration(lab, fixture, arguments.legacy_ref)
            except Exception as error:
                lab.check("historical migration oracle", False, error=str(error), traceback=traceback.format_exc())
    except Exception as error:
        lab.check("isolated acceptance setup", False, error=str(error), traceback=traceback.format_exc())
    finally:
        if fixture.thread.is_alive():
            fixture.close()
        lab.close()
        report = lab.report()
        print(json.dumps({key: report[key] for key in ("run", "passed", "failed", "unverified", "artifacts")}), flush=True)
    return 1 if report["failed"] or report["unverified"] else 0


if __name__ == "__main__":
    sys.exit(main())
