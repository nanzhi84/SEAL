"""Procrastinate alone owns persistent job scheduling, locking and recovery."""

import asyncio
from datetime import datetime, timezone

import procrastinate

from .db import connect, dsn, j, one

app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn()))


@app.task(
    name="seal.crawl",
    queue="seal",
    lock="seal:production",
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
        sources = c.execute(
            "SELECT * FROM seal_source WHERE binding_id IS NOT NULL AND NOT paused AND NOT needs_repair AND (cooldown_until IS NULL OR cooldown_until<=now()) ORDER BY id FOR UPDATE SKIP LOCKED"
        ).fetchall()
        for source in sources:
            now = datetime.now(timezone.utc)
            for mode, interval, due in (
                ("production", source["config"]["poll_seconds"], source["next_poll"] <= now),
                (
                    "recheck",
                    source["config"]["recheck_seconds"],
                    c.execute(
                        "SELECT 1 FROM seal_document WHERE source_id=%s AND namespace='production' AND next_check<=now() LIMIT 1",
                        (source["id"],),
                    ).fetchone()
                    is not None,
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
                if mode == "production":
                    c.execute(
                        "UPDATE seal_source SET next_poll=now()+(%s * interval '1 second') WHERE id=%s",
                        (interval, source["id"]),
                    )
                else:
                    c.execute(
                        "UPDATE seal_document SET next_check=now()+(%s * interval '1 second') WHERE source_id=%s AND namespace='production'",
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
                run = one(c, "SELECT * FROM seal_run WHERE id=%s FOR UPDATE", (run_id,))
                if run["status"] in ("complete", "partial", "superseded", "failed"):
                    return True  # one final task invocation reports the stored outcome
                if run["attempt_epoch"] >= run["max_attempts"] or run["deadline"] <= datetime.now(
                    timezone.utc
                ):
                    c.execute(
                        "UPDATE seal_run SET status='failed',completed_at=now(),errors=errors || %s WHERE id=%s",
                        (j(["attempts_or_deadline_exhausted"]), run_id),
                    )
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
