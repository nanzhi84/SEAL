"""Resumable national public permit enumeration; unfinished batches remain UNKNOWN.

No fixed sample is acceptance. Region discovery, every provincial coverage plan,
all page segments and every linked detail must pass before national status PASS.
Each command saves its own immutable Run/export. Retries retain failed evidence.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from live import write
from parsel import Selector

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--segment-pages", type=int, default=100)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.segment_pages <= 300:
        parser.error("segment-pages must be 1..300")
    args.output.mkdir(parents=True, exist_ok=True)
    objects = Path(os.environ["SEAL_ARCHIVE"]) / "objects"
    state_file = args.output / "coverage.json"
    state = (
        json.loads(state_file.read_text())
        if state_file.exists()
        else {
            "status": "RUNNING",
            "national_acceptance": "UNKNOWN",
            "regions": [],
            "partitions": [],
            "segments": [],
            "failed": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    def save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        write(state_file, state)

    def execute(label, params):
        retained = sorted(args.output.glob(label + "-attempt-*"))
        prior_attempts = []
        for out in retained:
            if not out.is_dir():
                continue
            prior_attempts.append(int(out.name.rsplit("-", 1)[1]))
            receipt = out / "dd-357" / "run.json"
            if receipt.exists():
                inspected = json.loads(receipt.read_text())
                if inspected["run"]["status"] == "complete":
                    exported = json.loads((out / "dd-357" / "export.json").read_text())
                    return inspected, exported, str(out / "dd-357")
        first_attempt = max(prior_attempts, default=0) + 1
        for attempt in range(first_attempt, first_attempt + 3):
            out = args.output / f"{label}-attempt-{attempt}"
            receipt = out / "dd-357" / "run.json"
            if not out.exists():
                parameter_file = args.output / f"{label}-params.json"
                write(parameter_file, params)
                with (args.output / f"{label}-attempt-{attempt}.log").open("w") as log:
                    subprocess.run(
                        [
                            sys.executable,
                            str(HERE / "business_live.py"),
                            "--output",
                            str(out),
                            "--sources",
                            "dd-357",
                            "--params-json",
                            str(parameter_file),
                        ],
                        stdout=log,
                        stderr=log,
                    )
            if receipt.exists():
                inspected = json.loads(receipt.read_text())
                if inspected["run"]["status"] == "complete":
                    exported = json.loads((out / "dd-357" / "export.json").read_text())
                    return inspected, exported, str(out / "dd-357")
                failure = {
                    "label": label,
                    "attempt": attempt,
                    "run_id": inspected["run"]["id"],
                    "errors": inspected["run"]["errors"],
                }
            else:
                failure = {"label": label, "attempt": attempt, "errors": ["missing_run_evidence"]}
            if failure not in state["failed"]:
                state["failed"].append(failure)
            save()
        raise RuntimeError("three retained failures: " + label)

    try:
        state["status"] = "RUNNING"
        state.pop("reason", None)
        save()
        if not state["regions"]:
            run, exported, path = execute("regions", {"permit_stage": "regions"})
            state["regions"] = [
                r["data"]
                for r in exported["records"]
                if r["record_type"] == "environmental_permit_region"
            ]
            state["regions_evidence"] = path
            save()
        planned = set(state.get("planned_provinces", []))
        for region in state["regions"]:
            code = region["regioncode"]
            if code in planned:
                continue
            run, exported, path = execute(
                "plan-" + code, {"permit_stage": "plan", "province": code}
            )
            partitions = []
            for record in exported["records"]:
                if record["record_type"] != "environmental_permit_partition":
                    continue
                parent = parse_qs(record["frozen_parent_request"]["body"], keep_blank_values=True)
                fields = {k: parent[k][0] for k in ("province", "city", "management")}
                sid = record["primary_snapshot_id"]
                snap = json.loads((objects / sid[:2] / sid[2:]).read_text())
                h = snap["body_hash"]
                rows = Selector(body=(objects / h[:2] / h[2:]).read_bytes()).css(
                    ".tabtd tr:not(.tbhead)"
                )
                pages = int(record["data"]["pages"])
                partitions.append(
                    {
                        **fields,
                        "pages": pages,
                        "records": (pages - 1) * 10 + len(rows),
                        "evidence": path,
                    }
                )
            if not partitions:
                raise RuntimeError("no proven partitions: " + code)
            state["partitions"].extend(partitions)
            planned.add(code)
            state["planned_provinces"] = sorted(planned)
            state["planned_records"] = sum(p["records"] for p in state["partitions"])
            save()
            print(
                json.dumps(
                    {
                        "province": region["regionname"],
                        "planned_provinces": len(planned),
                        "partitions": len(state["partitions"]),
                        "planned_records": state["planned_records"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        state["planning_status"] = "COMPLETE"
        save()
        if args.plan_only:
            return 0
        completed = {s["label"] for s in state["segments"]}
        for index, partition in enumerate(state["partitions"]):
            for first in range(1, partition["pages"] + 1, args.segment_pages):
                last = min(first + args.segment_pages - 1, partition["pages"])
                label = f"segment-{index:03}-{first:04}-{last:04}"
                if label in completed:
                    continue
                params = {
                    "permit_stage": "collect",
                    **{k: partition[k] for k in ("province", "city", "management")},
                    "first_page": first,
                    "last_page": last,
                }
                run, exported, path = execute(label, params)
                rows = [
                    r for r in exported["records"] if r["record_type"] == "environmental_permit_row"
                ]
                details = [
                    r
                    for r in exported["records"]
                    if r["record_type"] == "environmental_permit_detail"
                ]
                expected = (
                    10 * (last - first + 1)
                    if last < partition["pages"]
                    else partition["records"] - (first - 1) * 10
                )
                if len(rows) != expected or len(details) != expected:
                    raise RuntimeError("segment business population mismatch: " + label)
                state["segments"].append(
                    {
                        "label": label,
                        "partition": index,
                        "first_page": first,
                        "last_page": last,
                        "rows": len(rows),
                        "details": len(details),
                        "evidence": path,
                    }
                )
                state["collected_rows"] = sum(s["rows"] for s in state["segments"])
                state["collected_details"] = sum(s["details"] for s in state["segments"])
                save()
                print(
                    json.dumps(
                        {
                            "segment": label,
                            "rows": state["collected_rows"],
                            "details": state["collected_details"],
                            "planned_records": state["planned_records"],
                        }
                    ),
                    flush=True,
                )
        state["status"] = "COLLECTED_PENDING_INDEPENDENT_AUDIT"
        # Cross-partition identity, raw hashes, all pages and nested detail resources
        # still require independent verification; never infer acceptance from counts.
        save()
        return 0
    except Exception as exc:
        state["status"] = "STOPPED_WITH_FAILURE"
        state["reason"] = str(exc)
        save()
        print(json.dumps({"status": state["status"], "reason": state["reason"]}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
