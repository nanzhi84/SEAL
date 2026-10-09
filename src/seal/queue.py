"""Procrastinate alone owns persistent job scheduling, locking and recovery."""

import asyncio
from datetime import datetime, timezone

import procrastinate

from .db import connect, dsn, locked_run

app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn()))


@app.task(
    name="seal.crawl",
    queue="seal",
    lock="seal:runtime",
    retry=procrastinate.RetryStrategy(max_attempts=3, wait=5),
)
def crawl_job(run_id):
    from .runs import execute_run

    result = execute_run(run_id)
    if result["status"] == "retryable":
        raise RuntimeError("retryable_crawl_failure")


def enqueue(c, run_id):
    # .defer uses the connector's sync counterpart with this external transaction.
    job = crawl_job.configure(connection=c, queueing_lock="seal:run:" + run_id).defer(run_id=run_id)
    c.execute("UPDATE seal_run SET job_id=%s WHERE id=%s", (job, run_id))
    return job


def schedule_due():
    from .runs import create_run

    created = []
    with connect() as c:
        has_records = bool(
            c.execute("SELECT to_regclass('seal_record') AS name").fetchone()["name"]
        )
        sources = c.execute(
            "SELECT * FROM seal_source WHERE binding_id IS NOT NULL AND NOT paused AND (cooldown_until IS NULL OR cooldown_until<=now()) ORDER BY id FOR UPDATE SKIP LOCKED"
        ).fetchall()
        for source in sources:
            now = datetime.now(timezone.utc)
            for mode, interval, due in (
                ("collect", source["config"]["poll_seconds"], source["next_poll"] <= now),
                (
                    "recheck",
                    source["config"]["recheck_seconds"],
                    c.execute(
                        "SELECT 1 FROM seal_document d WHERE source_id=%s AND namespace='runtime' "
                        "AND next_check<=now() AND EXISTS "
                        "(SELECT 1 FROM seal_result r WHERE r.document_id=d.id) LIMIT 1",
                        (source["id"],),
                    ).fetchone()
                    is not None
                    or (
                        has_records
                        and c.execute(
                            "SELECT 1 FROM seal_record WHERE source_id=%s AND namespace='runtime' "
                            "AND latest_result IS NOT NULL AND next_check<=now() LIMIT 1",
                            (source["id"],),
                        ).fetchone()
                        is not None
                    ),
                ),
            ):
                if not due:
                    continue
                pending = c.execute(
                    "SELECT 1 FROM seal_run WHERE source_id=%s AND mode=%s AND status IN ('pending','running','finishing','retryable') LIMIT 1",
                    (source["id"], mode),
                ).fetchone()
                if pending:
                    continue
                slot = f"{source['id']}:{source['generation']}:{mode}:{int(now.timestamp()) // interval}"
                run_id = create_run(source["binding_id"], mode, c, slot=slot)
                enqueue(c, run_id)
                if mode == "collect":
                    c.execute(
                        "UPDATE seal_source SET next_poll=now()+(%s * interval '1 second') WHERE id=%s",
                        (interval, source["id"]),
                    )
                else:
                    c.execute(
                        "UPDATE seal_document d SET next_check=now()+(%s * interval '1 second') "
                        "WHERE source_id=%s AND namespace='runtime' AND EXISTS "
                        "(SELECT 1 FROM seal_result r WHERE r.document_id=d.id)",
                        (interval, source["id"]),
                    )
                    if has_records:
                        c.execute(
                            "UPDATE seal_record SET next_check=now()+(%s * interval '1 second') "
                            "WHERE source_id=%s AND namespace='runtime' AND latest_result IS NOT NULL",
                            (interval, source["id"]),
                        )
                created.append(run_id)
    return {"run_ids": created}


@app.periodic(cron="* * * * *")
@app.task(
    name="seal.schedule", queue="seal-control", lock="seal:schedule", queueing_lock="seal:schedule"
)
def periodic_schedule(timestamp):
    return schedule_due()


@app.periodic(cron="* * * * *")
@app.task(
    name="seal.recover", queue="seal-control", lock="seal:recover", queueing_lock="seal:recover"
)
async def recover_stalled(timestamp=0):
    jobs = await app.job_manager.get_stalled_jobs(
        task_name="seal.crawl", seconds_since_heartbeat=30
    )
    count = 0
    for job in jobs:
        run_id = job.task_kwargs["run_id"]

        def inspect_limit(run_id=run_id):
            with connect() as c:
                source, run = locked_run(c, run_id)
                if run["status"] in ("complete", "partial", "superseded", "failed"):
                    return True  # one final task invocation reports the stored outcome
                if run["attempt_epoch"] >= run["max_attempts"] or run["deadline"] <= datetime.now(
                    timezone.utc
                ):
                    from .runs import end_run

                    end_run(c, source, run, "failed", "attempts_or_deadline_exhausted")
                return True

        if await asyncio.to_thread(inspect_limit):
            await app.job_manager.retry_job(job)
            count += 1
    return {"recovered": count}


def worker(once=False):
    with app.open():
        app.run_worker(
            queues=["seal", "seal-control"],
            concurrency=1,
            wait=not once,
            update_heartbeat_interval=10,
            stalled_worker_timeout=30,
        )
