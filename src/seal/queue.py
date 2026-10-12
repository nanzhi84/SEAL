"""Procrastinate alone owns persistent job scheduling, locking and recovery."""

import asyncio

import procrastinate

from .db import connect, dsn, locked_run
from .run_state import TERMINAL_STATES, exhaustion_error

app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn()))
producer = procrastinate.App(connector=procrastinate.SyncPsycopgConnector(conninfo=dsn()))


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
    # Worker recovery runs alongside an async consumer. Its external Source/Run
    # transaction is synchronous; never pass it to the consumer's async cursor.
    job = crawl_job.configure(queueing_lock="seal:run:" + run_id).make_new_job(run_id=run_id)
    job = producer.job_manager.defer_job(job=job, connection=c)
    c.execute("UPDATE seal.run SET job_id=%s WHERE id=%s", (job.id, run_id))
    return job.id


@app.periodic(cron="* * * * *")
@app.task(
    name="seal.recover", queue="seal-control", lock="seal:recover", queueing_lock="seal:recover"
)
async def recover_stalled(timestamp=0):
    await asyncio.to_thread(recover_unqueued)
    jobs = await app.job_manager.get_stalled_jobs(
        task_name="seal.crawl", seconds_since_heartbeat=30
    )
    count = 0
    for job in jobs:
        run_id = job.task_kwargs["run_id"]

        def inspect_limit(run_id=run_id):
            with connect() as c:
                source, run = locked_run(c, run_id)
                if run["status"] in TERMINAL_STATES:
                    return True  # one final task invocation reports the stored outcome
                limit = exhaustion_error(run)
                if limit:
                    from .runs import end_run

                    end_run(c, source, run, "failed", limit)
                return True

        if await asyncio.to_thread(inspect_limit):
            await app.job_manager.retry_job(job)
            count += 1
    return {"recovered": count}


def recover_unqueued():
    with connect() as c:
        rows = c.execute(
            "SELECT id FROM seal.run WHERE job_id IS NULL AND status IN ('pending','running','finishing','retryable') ORDER BY created_at"
        ).fetchall()
        for row in rows:
            key = row["id"]
            if c.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired", (key,)
            ).fetchone()["acquired"]:
                _, run = locked_run(c, key)
                if run["job_id"] is None and run["status"] not in TERMINAL_STATES:
                    enqueue(c, key)


def worker(once=False):
    async def recover_on_start():
        async with app.open_async():
            await recover_stalled()

    asyncio.run(recover_on_start())
    with app.open():
        app.run_worker(
            queues=["seal", "seal-control"],
            concurrency=1,
            wait=not once,
            update_heartbeat_interval=10,
            stalled_worker_timeout=30,
        )
