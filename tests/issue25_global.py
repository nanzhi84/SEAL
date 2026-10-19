"""Cross-Run process admission, shared cleanup failure, and late native failure."""

import argparse
import io
import json
import os
import subprocess
import sys
import time

from pypdf import PdfWriter
from pypdf.generic import EncodedStreamObject, NameObject

from issue25_engine import install, read_events
from issue25_faults import assert_cleanup, codes, versions
from issue25_fixtures import FixtureServer, fixtures, native_page, writer_bytes
from issue25_lab import Lab, ROOT, implementation_digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="issue25-global-" + time.strftime("%Y%m%d_%H%M%S"))
    args = parser.parse_args()
    lab = Lab(args.name)
    try:
        data = fixtures(lab.work / "inputs")
        data.update({"scan_" + str(i): data["scan"] for i in range(8)})
        bad = PdfWriter()
        native_page(bad, "FIRST NATIVE PAGE TEXT")
        page = native_page(bad, "SECOND NATIVE PAGE")
        invalid_stream = EncodedStreamObject()
        invalid_stream._data = b"invalid"
        invalid_stream[NameObject("/Filter")] = NameObject("/SEALInvalidFilter")
        page[NameObject("/Contents")] = bad._add_object(invalid_stream)
        data["native_later_corrupt"] = writer_bytes(bad)
        lab.cli("init")
        with FixtureServer(data) as server:
            hook, log = install(lab.work / "cross-run", "slow", kind="worker")
            env = dict(lab.env, **hook)
            processes = []
            for index, source_id in enumerate(["dd-200", "dd-201"]):
                urls = [server.base + "/scan_" + str(i) + ".pdf" for i in range(index * 4, index * 4 + 4)]
                source = lab.source(source_id, urls, seconds=45)
                output = open(lab.work / (source_id + ".stdout"), "wb")
                error = open(lab.work / (source_id + ".stderr"), "wb")
                process = subprocess.Popen([sys.executable, "-m", "seal", "collect", str(source), "--no-progress"],
                                           env=env, cwd=ROOT, stdout=output, stderr=error)
                processes.append((process, output, error, source_id))
            for process, output, error, source_id in processes:
                process.wait(timeout=55)
                output.close()
                error.close()
                lab.check(source_id + ":all_four_successful_versions", versions(lab, source_id) == 4)
            events = assert_cleanup(lab, "cross_run", log)
            changes = [(e["at"], 1 if e["kind"] == "worker_start" else -1)
                       for e in events if e["kind"] in {"worker_start", "worker_output"}]
            active = peak = 0
            for _, change in sorted(changes):
                active += change
                peak = max(peak, active)
            lab.check("cross_run:global_max_four", 1 <= peak <= 4,
                      {"peak": peak, "workers": sum(e["kind"] == "worker_start" for e in events)})
            parents = {e["ppid"] for e in events if e["kind"] == "worker_start"}
            lab.check("cross_run:independent_collect_processes", len(parents) == 2)

            source = lab.source("dd-204", [server.base + "/native_later_corrupt.pdf"])
            _, receipt = lab.cli("collect", source, "--no-progress")
            actual = codes(lab, "dd-204", receipt)
            lab.check("later_native_extract_exception:stable_reason", "pdf_invalid_or_encrypted" in actual, sorted(actual))
            lab.check("later_native_extract_exception:no_version", versions(lab, "dd-204") == 0)

            hook, log = install(lab.work / "cleanup-failure", "slow", kind="worker", hook_mode="cleanup_failure")
            source = lab.source("dd-202", [server.base + "/blocked/scan.pdf"], seconds=15)
            _, receipt = lab.cli("collect", source, "--no-progress", env=dict(lab.env, **hook))
            lab.check("cleanup_failure:stable_reason", "pdf_cleanup_failed" in codes(lab, "dd-202", receipt))
            lab.check("cleanup_failure:no_version", versions(lab, "dd-202") == 0)
            assert_cleanup(lab, "cleanup_failure", log, allow_retained=True)
            hook, log = install(lab.work / "after-failure", "slow", kind="worker")
            source = lab.source("dd-203", [server.base + "/next/scan.pdf"])
            _, receipt = lab.cli("collect", source, "--no-progress", env=dict(lab.env, **hook))
            lab.check("cross_run_failure:stable_reason", "pdf_cleanup_failed" in codes(lab, "dd-203", receipt))
            lab.check("cross_run_failure:no_worker_started", not read_events(log))
            lab.check("cross_run_failure:no_version", versions(lab, "dd-203") == 0)
        lab.check("implementation_frozen_during_acceptance", lab.environment["implementation_sha256"] == implementation_digest())
        failed = sum(c["status"] == "FAIL" for c in lab.checks)
        print(json.dumps({"passed": len(lab.checks) - failed, "failed": failed, "report": str(lab.out / "report.json")}))
        return bool(failed)
    finally:
        lab.close()


if __name__ == "__main__":
    raise SystemExit(main())
