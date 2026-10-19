"""Explicit cleanup limited to resources registered by this acceptance session."""

import argparse
import json
import re
from pathlib import Path

import boto3
import psycopg

from issue25_engine import read_events
from issue25_faults import live_processes
from issue25_lab import ROOT, local_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    scope = json.loads(args.scope.read_text())
    all_pids = set()
    for row in scope:
        if not re.fullmatch(r"seal_issue25_[a-f0-9]{10}", row["database"]):
            raise ValueError("unsafe database scope")
        if row["bucket"] != row["database"].replace("seal_issue25_", "seal-issue25-"):
            raise ValueError("unsafe bucket scope")
        report = json.loads((ROOT / row["report"]).read_text())
        if any(row[key] != report[key] for key in ["database", "bucket"]):
            raise ValueError("scope does not match report")
        for path in (ROOT / "work" / report["run"]).rglob("events.jsonl"):
            for event in read_events(path):
                all_pids.update(event[key] for key in ["pid", "pgid"] if key in event)
    live = live_processes(all_pids)
    if live:
        raise RuntimeError("acceptance process group still present: " + json.dumps(live))
    env = local_settings()
    s3 = boto3.client("s3", endpoint_url=env["SEAL_S3_ENDPOINT"],
                      aws_access_key_id=env.get("MINIO_ROOT_USER", env["SEAL_S3_ACCESS_KEY"]),
                      aws_secret_access_key=env.get("MINIO_ROOT_PASSWORD", env["SEAL_S3_SECRET_KEY"]))
    results = []
    try:
        with psycopg.connect(env["SEAL_DATABASE_URL"], autocommit=True) as connection:
            for row in scope:
                pages = s3.get_paginator("list_objects_v2").paginate(Bucket=row["bucket"])
                keys = [item["Key"] for page in pages for item in page.get("Contents", [])]
                exists = connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (row["database"],)).fetchone() is not None
                item = dict(row, s3_objects=len(keys), database_present=exists)
                if args.execute:
                    for start in range(0, len(keys), 1000):
                        deleted = s3.delete_objects(Bucket=row["bucket"], Delete={"Objects": [{"Key": key} for key in keys[start:start + 1000]], "Quiet": True})
                        if deleted.get("Errors"):
                            raise RuntimeError("S3 test object cleanup failed")
                    s3.delete_bucket(Bucket=row["bucket"])
                    if exists:
                        connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (row["database"],))
                        connection.execute(psycopg.sql.SQL("DROP DATABASE {}").format(psycopg.sql.Identifier(row["database"])))
                    item["database_removed"] = connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (row["database"],)).fetchone() is None
                    current_buckets = {entry["Name"] for entry in s3.list_buckets()["Buckets"]}
                    item["bucket_removed"] = row["bucket"] not in current_buckets
                results.append(item)
    finally:
        s3.close()
    output = args.scope.with_name("cleanup.json" if args.execute else "cleanup-preview.json")
    output.write_text(json.dumps({"mode": "execute" if args.execute else "preview", "live_acceptance_processes": live,
                                  "process_pids_and_groups_checked": len(all_pids), "resources": results}, indent=2))
    print(json.dumps({"resources": len(results), "objects": sum(r["s3_objects"] for r in results), "output": str(output), "live_processes": len(live)}))


if __name__ == "__main__":
    main()
