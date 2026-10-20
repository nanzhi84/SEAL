"""Adapter Runtime: persistent request tasks and document targets with attempt fencing.

Three completion points stay distinct: an input is archived (request task ``archived``),
a document target is registered (target row) and every target has a success or failure
delivery (target ``done``/``failed``). Follow-up requests and targets are registered in
the same transaction that completes their parent request, before any watermark moves.
"""

import asyncio
import os
import signal
import sys
import threading
import time

from .adapters import load
from .core import SealError, digest, uid
from .db import connect, fenced, j, locked_run, record_error
from .document import build
from .objects import Objects
from .parsing import require_runtime
from .pdf_runtime import run_extraction
from .quality import text_check
from .resources import commit, fail, save_checkpoint
from .runs import run_context
from .transport import Budget, Recorder, Transport, request_key

CHECKPOINT_SCHEMA = "seal.checkpoint.v1"
ADAPTER_STATE_SCHEMA = 1


def adapter_error(exc, prefix):
    """Stable error code; source-specific or unexpected failures never crash the Run."""
    if isinstance(exc, SealError):
        if exc.code in {
            "interrupted",
            "crawl_time_budget_exceeded",
            "stale_attempt",
            "inactive_attempt",
            "run_not_started",
            "deadline_exceeded",
        }:
            raise exc
        return exc.code
    return prefix + ":" + type(exc).__name__


def target_key(target):
    identity = target.get("identity") or {}
    if identity.get("namespace") and identity.get("external_key"):
        return digest(
            {"namespace": identity["namespace"], "external_key": identity["external_key"]}
        )
    return digest({"inputs": [request_key(item["request"]) for item in target["inputs"]]})


class Context:
    """The only capabilities an Adapter receives: archived reads and bounded extraction."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.config = runtime.config
        self.objects = Objects()

    def read(self, acquisition):
        if acquisition.get("state") != "archived":
            raise SealError("input_not_archived")
        return self.objects.get(acquisition["archive_ref"])

    def store(self, data):
        ref = self.objects.put(data)
        self.objects.get(ref)
        return ref

    def extract(self, data, kind):
        return run_extraction(data, kind, self.config, active=self.runtime.budget.active)

    def text_quality(self, markdown):
        language = str(self.config["metadata"].get("language") or "")
        return text_check(markdown, "vi" if language.startswith("vi") else "generic")

    def follow_links(self, task):
        return self.runtime.mode == "collect" and task["depth"] < self.config["fetch"]["max_depth"]


class Runtime:
    def __init__(self, context):
        self.run = context
        self.run_id = context["id"]
        self.epoch = context["attempt_epoch"]
        self.config = context["config"]
        self.mode = context["mode"]
        self.checkpoint = context["checkpoint"] or {}
        with connect() as c:
            used = c.execute(
                "SELECT count(*) AS n FROM seal.acquisition WHERE run_id=%s", (self.run_id,)
            ).fetchone()["n"]
        self.budget = Budget(self.config, used, context["deadline"])
        self.recorder = Recorder(self.run_id, self.epoch, context["source_id"])
        self.transport = Transport(self.config, self.budget, self.recorder)
        self.browser = None
        if self.config["fetch"]["mode"] == "rendered_dom":
            from .browser import Browser

            self.browser = Browser(self.config, self.budget, self.recorder, self.transport)
        self.adapter = load(self.config["adapter"])(self.config)
        self.ctx = Context(self)
        self.truncated = None

    # ---- persistence -------------------------------------------------------------------

    def write(self, function):
        with connect() as c:
            source, run = locked_run(c, self.run_id)
            fenced(source, run, self.epoch)
            return function(c)

    def insert_request(self, c, request, depth):
        key = request_key(request)
        payload = {
            k: request[k]
            for k in ("url", "method", "form", "role", "idempotent", "meta")
            if k in request
        }
        row = c.execute(
            """INSERT INTO seal.task(id,run_id,kind,key,payload,depth) VALUES(%s,%s,'request',
            %s,%s,%s) ON CONFLICT(run_id,kind,key) DO NOTHING RETURNING id""",
            (uid(), self.run_id, key, j(payload), depth),
        ).fetchone()
        if row:
            return row["id"]
        return c.execute(
            "SELECT id FROM seal.task WHERE run_id=%s AND kind='request' AND key=%s",
            (self.run_id, key),
        ).fetchone()["id"]

    def register(self, result, parent=None, depth=0):
        """Persist follow-up requests/targets and complete the parent in one transaction."""

        def apply(c):
            for request in result.get("requests", []):
                self.insert_request(c, request, request.get("depth", depth))
            for target in result.get("targets", []):
                key = target_key(target)
                count = c.execute(
                    "SELECT count(*) AS n FROM seal.task WHERE run_id=%s AND kind='target'",
                    (self.run_id,),
                ).fetchone()["n"]
                exists = c.execute(
                    """SELECT * FROM seal.task WHERE run_id=%s AND kind='target' AND key=%s
                    FOR UPDATE""",
                    (self.run_id, key),
                ).fetchone()
                if exists:
                    if exists["state"] == "pending":
                        self.amend(c, exists, target, depth)
                    continue
                state = "pending"
                if count >= self.config["budget"]["documents"]:
                    state = "skipped"
                payload = {
                    "identity": target.get("identity"),
                    "context": target.get("context") or {},
                    "inputs": [
                        {
                            **{k: v for k, v in item.items() if k != "request"},
                            "request_key": request_key(item["request"]),
                        }
                        for item in target["inputs"]
                    ],
                }
                target_id = uid()
                c.execute(
                    """INSERT INTO seal.task(id,run_id,kind,key,payload,state,error)
                    VALUES(%s,%s,'target',%s,%s,%s,%s)""",
                    (
                        target_id,
                        self.run_id,
                        key,
                        j(payload),
                        state,
                        "document_budget_exceeded" if state == "skipped" else None,
                    ),
                )
                if state == "skipped":
                    self.truncated = "document_budget_exceeded"
                    continue
                for position, item in enumerate(target["inputs"]):
                    request_id = self.insert_request(
                        c, item["request"], item["request"].get("depth", depth)
                    )
                    c.execute(
                        """INSERT INTO seal.task_input(target_id,request_id,position)
                        VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (target_id, request_id, position),
                    )
            if parent is not None:
                c.execute(
                    """UPDATE seal.task SET state='done',attempt_epoch=%s,
                    updated_at=clock_timestamp() WHERE id=%s""",
                    (self.epoch, parent),
                )
            if "state" in result:
                self.checkpoint["adapter_state"] = {
                    "schema": ADAPTER_STATE_SCHEMA,
                    **(result["state"] or {}),
                }

        self.write(apply)

    def amend(self, c, row, target, depth):
        """A not-yet-delivered target gains newly discovered required inputs."""
        payload = row["payload"]
        known = {item["request_key"] for item in payload["inputs"]}
        added = False
        for item in target["inputs"]:
            key = request_key(item["request"])
            if key in known:
                continue
            request_id = self.insert_request(
                c, item["request"], item["request"].get("depth", depth)
            )
            c.execute(
                """INSERT INTO seal.task_input(target_id,request_id,position) VALUES(%s,%s,%s)
                ON CONFLICT DO NOTHING""",
                (row["id"], request_id, len(payload["inputs"])),
            )
            payload["inputs"].append(
                {**{k: v for k, v in item.items() if k != "request"}, "request_key": key}
            )
            known.add(key)
            added = True
        if added:
            c.execute(
                "UPDATE seal.task SET payload=%s,updated_at=clock_timestamp() WHERE id=%s",
                (j(payload), row["id"]),
            )

    def mark_request(self, task_id, state, acquisition_id=None, error=None):
        self.write(
            lambda c: c.execute(
                """UPDATE seal.task SET state=%s,acquisition_id=%s,error=%s,attempt_epoch=%s,
                updated_at=clock_timestamp() WHERE id=%s""",
                (state, acquisition_id, error, self.epoch, task_id),
            )
        )

    def save(self, **values):
        self.checkpoint.update(values)
        self.checkpoint["schema"] = CHECKPOINT_SCHEMA
        save_checkpoint(self.run_id, self.epoch, self.checkpoint)

    def rows(self, query, params=()):
        with connect() as c:
            return c.execute(query, params).fetchall()

    def acquisition(self, identity):
        with connect() as c:
            return c.execute("SELECT * FROM seal.acquisition WHERE id=%s", (identity,)).fetchone()

    def reuse(self, key):
        """An archived final response from an earlier attempt is re-read, never refetched."""
        with connect() as c:
            return c.execute(
                """SELECT * FROM seal.acquisition WHERE run_id=%s AND request_key=%s
                AND role='content' AND state='archived' AND outcome<>'redirect'
                ORDER BY acquired_at DESC LIMIT 1""",
                (self.run_id, key),
            ).fetchone()

    # ---- phases ------------------------------------------------------------------------

    def plan(self):
        if self.checkpoint.get("planned"):
            return
        state = (self.checkpoint.get("adapter_state") or {}).copy()
        state.pop("schema", None)
        if self.mode == "recheck":
            resources = (self.run.get("recheck_plan") or {}).get("resources", [])
            result = self.adapter.plan_recheck(resources, state)
        else:
            result = self.adapter.plan(state)
        self.register(result)
        self.save(planned=True)

    async def fetch(self, task):
        request = task["payload"]
        previous = self.reuse(task["key"])
        if previous:
            return previous
        if (
            self.browser is not None
            and request.get("role", "page") == "page"
            and request.get("method", "GET") == "GET"
        ):
            return await self.browser.acquire(request)
        return await self.transport.acquire(request)

    async def fetch_batch(self):
        tasks = self.rows(
            """SELECT * FROM seal.task WHERE run_id=%s AND kind='request' AND state='pending'
            ORDER BY seq LIMIT %s""",
            (self.run_id, self.config["concurrency"]),
        )
        if not tasks:
            return False
        results = await asyncio.gather(*(self.fetch(t) for t in tasks), return_exceptions=True)
        for task, result in zip(tasks, results, strict=True):
            if isinstance(result, SealError):
                if result.code in {"request_budget_exceeded", "crawl_time_budget_exceeded"}:
                    self.truncated = result.code
                    continue
                if result.code in {"interrupted", "stale_attempt", "inactive_attempt"}:
                    raise result
                self.mark_request(task["id"], "failed", error=result.code)
                record_error(self.run_id, self.epoch, result.code)
            elif isinstance(result, BaseException):
                raise result
            elif result["state"] == "archived":
                self.mark_request(task["id"], "archived", result["id"])
            else:
                self.mark_request(task["id"], "failed", result["id"], result["error"])
                if result["state"] == "cancelled":
                    raise SealError("interrupted")
        return True

    def parse_archived(self):
        tasks = self.rows(
            """SELECT * FROM seal.task WHERE run_id=%s AND kind='request' AND state='archived'
            ORDER BY seq""",
            (self.run_id,),
        )
        for task in tasks:
            self.budget.active()
            acquisition = self.acquisition(task["acquisition_id"])
            try:
                result = self.adapter.parse(self.ctx, task, acquisition)
            except (SealError, Exception) as exc:
                code = adapter_error(exc, "adapter_parse_failed")
                self.write(
                    lambda c, t=task, e=code: c.execute(
                        """UPDATE seal.task SET state='failed',error=%s,attempt_epoch=%s,
                        updated_at=clock_timestamp() WHERE id=%s""",
                        (e, self.epoch, t["id"]),
                    )
                )
                record_error(self.run_id, self.epoch, code)
                continue
            self.register(result, parent=task["id"], depth=task["depth"] + 1)
        return bool(tasks)

    def ready_targets(self):
        return self.rows(
            """SELECT t.* FROM seal.task t WHERE t.run_id=%s AND t.kind='target'
            AND t.state='pending' AND NOT EXISTS (
              SELECT 1 FROM seal.task_input i JOIN seal.task r ON r.id=i.request_id
              WHERE i.target_id=t.id AND r.state NOT IN ('done','failed','skipped'))
            ORDER BY t.seq""",
            (self.run_id,),
        )

    def target_inputs(self, target):
        return self.rows(
            """SELECT r.*, i.position FROM seal.task_input i JOIN seal.task r ON r.id=i.request_id
            WHERE i.target_id=%s ORDER BY i.position""",
            (target["id"],),
        )

    def deliver(self, target):
        identity = target["payload"].get("identity")
        inputs, acquisitions = [], {}
        rows = {row["key"]: row for row in self.target_inputs(target)}
        for meta in target["payload"]["inputs"]:
            row = rows[meta["request_key"]]
            acquisition = self.acquisition(row["acquisition_id"]) if row["acquisition_id"] else None
            if (
                acquisition is None
                or acquisition["state"] != "archived"
                or acquisition["outcome"] != "ok"
            ):
                code = (acquisition or {}).get("error") or row["error"] or "input_unavailable"
                return fail(
                    self.run_id,
                    self.epoch,
                    identity,
                    "required_input_failed:" + code,
                    acquisition_ids=[a["id"] for a in acquisitions.values()]
                    + ([acquisition["id"]] if acquisition else []),
                    target_id=target["id"],
                )
            acquisitions[acquisition["id"]] = acquisition
            inputs.append({**meta, "task": row, "acquisition": acquisition})
        try:
            candidates = self.adapter.build(self.ctx, target["payload"], inputs)
            if len(candidates) != 1:
                raise SealError("target_must_build_one_document")
            built = build(
                Objects(),
                source_id=self.run["source_id"],
                run_id=self.run_id,
                manifest=self.run["parser_manifest"],
                candidate=candidates[0],
                acquisitions=acquisitions,
            )
        except (SealError, Exception) as exc:
            code = adapter_error(exc, "adapter_build_failed")
            if code in {"interrupted", "crawl_time_budget_exceeded", "stale_attempt"}:
                raise
            return fail(
                self.run_id,
                self.epoch,
                identity,
                code,
                acquisition_ids=list(acquisitions),
                target_id=target["id"],
            )
        self.budget.active()
        result = commit(
            self.run_id, self.epoch, built, target_id=target["id"], check_active=self.budget.active
        )
        return result

    def deliver_ready(self):
        targets = self.ready_targets()
        for target in targets:
            self.budget.active()
            self.deliver(target)
        return bool(targets)

    def close_unfinished(self):
        """Targets that can no longer complete are reported, not invented as failures."""
        reason = self.truncated or "inputs_unavailable"
        self.write(
            lambda c: c.execute(
                """UPDATE seal.task SET state='skipped',error=%s,attempt_epoch=%s,
                updated_at=clock_timestamp() WHERE run_id=%s AND kind='target' AND state='pending'""",
                (reason, self.epoch, self.run_id),
            )
        )

    def report(self, finish_reason):
        with connect() as c:
            tasks = c.execute(
                """SELECT kind,state,count(*) AS n FROM seal.task WHERE run_id=%s
                GROUP BY kind,state""",
                (self.run_id,),
            ).fetchall()
            outcomes = c.execute(
                "SELECT outcome,count(*) AS n FROM seal.observation WHERE run_id=%s GROUP BY outcome",
                (self.run_id,),
            ).fetchall()
            requests = c.execute(
                "SELECT count(*) AS n FROM seal.acquisition WHERE run_id=%s", (self.run_id,)
            ).fetchone()["n"]
        counts = {"request": {}, "target": {}}
        for row in tasks:
            counts[row["kind"]][row["state"]] = row["n"]
        pending = counts["request"].get("pending", 0)
        complete = finish_reason == "finished" and not self.truncated and not pending
        return {
            "adapter": self.config["adapter"],
            "finish_reason": finish_reason,
            "external_requests": requests,
            "request_tasks": counts["request"],
            "targets": counts["target"],
            "observations": {r["outcome"]: r["n"] for r in outcomes},
            "truncated": self.truncated,
            "coverage": {
                "selected_scope_enumeration": "complete" if complete else "incomplete",
                "site_population": "UNKNOWN",
                "unknown_coverage": True,
                "absence_semantics": "unobserved_is_unknown_never_deleted",
            },
        }

    async def execute(self):
        finish_reason = "finished"
        try:
            self.plan()
            while True:
                self.budget.active()
                progressed = self.parse_archived()
                progressed |= self.deliver_ready()
                if self.truncated:
                    break
                fetched = await self.fetch_batch()
                if not (progressed or fetched):
                    break
            self.parse_archived()
            self.deliver_ready()
            self.close_unfinished()
            self.save(watermark=self.watermark())
        except SealError as exc:
            if exc.code in {"stale_attempt", "inactive_attempt", "run_not_started"}:
                raise
            finish_reason = "interrupted" if exc.code == "interrupted" else "stopped"
            if exc.code == "crawl_time_budget_exceeded":
                self.truncated = exc.code
                finish_reason = "finished"
                try:
                    self.close_unfinished()
                except SealError:
                    pass
            if exc.code != "interrupted":
                record_error(self.run_id, self.epoch, exc.code)
        finally:
            await self.transport.close()
            if self.browser is not None:
                await self.browser.close()
        report = self.report(finish_reason)
        if self.truncated and self.truncated != "document_budget_exceeded":
            record_error(self.run_id, self.epoch, self.truncated)
        with connect() as c:
            c.execute(
                """UPDATE seal.run SET status='finishing',report=report || %s
                WHERE id=%s AND attempt_epoch=%s AND status='running'""",
                (j(report), self.run_id, self.epoch),
            )

    def watermark(self):
        with connect() as c:
            row = c.execute(
                """SELECT max(seq) AS seq FROM seal.task WHERE run_id=%s
                AND state IN ('done','failed','skipped')""",
                (self.run_id,),
            ).fetchone()
        return row["seq"] or 0


def watch_parent(parent):
    while True:
        if os.getppid() != parent:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        time.sleep(0.2)


async def run(context):
    try:
        require_runtime(context["config"], context["parser_manifest"])
    except SealError as exc:
        record_error(context["id"], context["attempt_epoch"], exc.code)
        with connect() as c:
            c.execute(
                """UPDATE seal.run SET status='finishing' WHERE id=%s AND attempt_epoch=%s
                AND status='running'""",
                (context["id"], context["attempt_epoch"]),
            )
        return
    runtime = Runtime(context)

    def cancel(signum, frame):
        runtime.budget.cancelled = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, cancel)
    await runtime.execute()


def main():
    run_id, epoch, parent = sys.argv[1:]
    threading.Thread(target=watch_parent, args=(int(parent),), daemon=True).start()
    asyncio.run(run(run_context(run_id, int(epoch))))


if __name__ == "__main__":
    main()
