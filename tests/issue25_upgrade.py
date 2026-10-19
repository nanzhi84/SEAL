"""Actual pre-change native collection -> new page provenance Version -> unchanged."""

import argparse
import io
import json
import subprocess
import tarfile
import time
import urllib.parse

import yaml

from issue25_fixtures import FixtureServer, fixtures
from issue25_lab import Lab, ROOT, implementation_digest


def version_rows(lab):
    with lab.connect() as conn:
        return conn.execute("""SELECT v.id,v.sequence,v.result_ref FROM seal.version v
            JOIN seal.resource r ON r.id=v.resource_id JOIN seal.frontier f ON f.id=r.frontier_id
            WHERE f.source_id='dd-040' ORDER BY v.sequence""").fetchall()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="issue25-upgrade-" + time.strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--baseline", default="c8e8ec1ae35d71064da9bb12b0a1ba44d5f90d70")
    args = parser.parse_args()
    lab = Lab(args.name)
    try:
        data = fixtures(lab.work / "inputs")
        archive = subprocess.check_output(["git", "archive", args.baseline, "src/seal"], cwd=ROOT)
        old = lab.work / "before"
        old.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(old, filter="data")
        head = subprocess.check_output(["git", "rev-parse", args.baseline], cwd=ROOT, text=True).strip()
        lab.check("prior_revision_recorded", bool(head), {"revision": head})
        lab.cli("init")
        with FixtureServer(data) as server:
            url = server.base + "/native.pdf"
            source = lab.source("dd-040", [url])
            config = yaml.safe_load(source.read_text())
            config.pop("ocr")
            source.write_text(yaml.safe_dump(config))
            old_env = dict(lab.env, PYTHONPATH=str(old / "src"))
            lab.cli("collect", source, "--no-progress", env=old_env)
            first = version_rows(lab)
            lab.check("prior_native_successful_version", len(first) == 1)
            if len(first) != 1:
                return 1
            first_object = json.loads(lab.object(first[0]["result_ref"]))
            first_metadata = first_object["result"].get("metadata") or {}
            lab.check("prior_native_contract", first_metadata.get("pdf_text_contract") == "crawl4ai_0_9_5_native_v1",
                      {"actual": first_metadata.get("pdf_text_contract")})
            _, frozen = lab.cli("snapshot", "create")
            old_snapshot = frozen["snapshot_id"]
            lab.cli("snapshot", "publish", old_snapshot)
            lab.start_api()
            path = "/results?" + urllib.parse.urlencode({"source_url": url})
            original_api = lab.get(path)
            lab.cli("collect", source, "--no-progress")
            second = version_rows(lab)
            lab.check("new_native_contract_adds_one_version", len(second) == 2 and [r["sequence"] for r in second] == [1, 2])
            if len(second) != 2:
                return 1
            second_object = json.loads(lab.object(second[-1]["result_ref"]))
            metadata = second_object["result"].get("metadata") or {}
            lab.check("new_native_page_contract", metadata.get("pdf_text_contract") == "crawl4ai_0_9_5_native_pages_v1")
            lab.check("new_native_all_page_provenance", [p.get("page_number") for p in metadata.get("pdf", {}).get("pages", [])] == [1, 2, 3])
            lab.check("old_version_object_unchanged", lab.object(first[0]["result_ref"]) == json.dumps(first_object, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
            lab.cli("collect", source, "--no-progress")
            third = version_rows(lab)
            lab.check("repeat_is_unchanged_no_new_version", third == second)
            with lab.connect() as conn:
                last = conn.execute("""SELECT o.outcome,o.version_id FROM seal.observation o
                    JOIN seal.resource r ON r.id=o.resource_id JOIN seal.frontier f ON f.id=r.frontier_id
                    WHERE f.source_id='dd-040' ORDER BY o.observed_at DESC LIMIT 1""").fetchone()
                frozen_version = conn.execute("SELECT version_id FROM seal.snapshot_item WHERE snapshot_id=%s", (old_snapshot,)).fetchone()
            lab.check("unchanged_observation_reuses_current", last["outcome"] == "unchanged" and last["version_id"] == second[-1]["id"])
            lab.check("old_snapshot_keeps_prior_version", frozen_version["version_id"] == first[0]["id"])
            current_api = lab.get(path)
            lab.check("published_old_snapshot_keeps_original_metadata", current_api[0]["metadata"] == original_api[0]["metadata"])
            _, newer = lab.cli("snapshot", "create")
            lab.cli("snapshot", "publish", newer["snapshot_id"])
            new_api = lab.get(path)
            lab.check("new_snapshot_exposes_new_page_provenance", new_api[0]["metadata"].get("pdf", {}).get("method") == "native")
            lab.check("exactly_one_download_per_collection", server.requests["/native.pdf"] == 3)

            pending = lab.source("dd-041", [server.base + "/pending/native.pdf"])
            pending_config = yaml.safe_load(pending.read_text())
            pending_config.pop("ocr")
            pending.write_text(yaml.safe_dump(pending_config))
            _, queued = lab.cli("collect", pending, "--enqueue", env=old_env)
            pending_id = queued["run_id"]
            with lab.connect() as conn:
                before = conn.execute("SELECT config,config_hash FROM seal.run WHERE id=%s", (pending_id,)).fetchone()
            lab.check("old_pending_run_lacks_ocr", "ocr" not in before["config"])
            lab.cli("worker", "--once", timeout=60)
            with lab.connect() as conn:
                after = conn.execute("SELECT config,config_hash,status FROM seal.run WHERE id=%s", (pending_id,)).fetchone()
                count = conn.execute("SELECT count(*) AS n FROM seal.observation WHERE run_id=%s AND version_id IS NOT NULL", (pending_id,)).fetchone()["n"]
            lab.check("old_pending_run_current_worker_success", after["status"] == "complete" and count == 1)
            lab.check("old_pending_run_frozen_config_hash_unchanged", after["config_hash"] == before["config_hash"] and after["config"] == before["config"])
        lab.check("implementation_frozen_during_acceptance", lab.environment["implementation_sha256"] == implementation_digest())
        failed = sum(c["status"] == "FAIL" for c in lab.checks)
        print(json.dumps({"passed": len(lab.checks) - failed, "failed": failed, "report": str(lab.out / "report.json")}))
        return bool(failed)
    finally:
        lab.close()


if __name__ == "__main__":
    raise SystemExit(main())
