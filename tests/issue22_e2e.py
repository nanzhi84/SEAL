"""Run: uv run --no-sync python tests/issue22_e2e.py

Failure modes were frozen before test implementation and preserved as stable input
tests/issue22_failure_boundaries.json. Requires Docker daemon,
local postgres:17-alpine, pinned issue20 MinIO/mc images and uv frozen runtime.
Uses only fresh random isolated services, real collector/HTTP/SQL/S3 processes.
"""
from __future__ import annotations

import traceback

from issue22_configuration import configuration
from issue22_contract import (
    attachments,
    authentication,
    check_native,
    pagination,
    revalidation,
    timeline,
)
from issue22_data import Data
from issue22_faults import archive_faults, dependencies, limits, readonly, timeout_and_busy
from issue22_lab import ROOT, Lab, write


def main():
    lab = Lab()
    data = Data(lab)
    try:
        lab.start()
        data.collect_initial()
        data.setup_storage_boundaries()
        (lab.artifacts / "failure-boundaries.json").write_bytes(
            (ROOT / "tests/issue22_failure_boundaries.json").read_bytes())
        lab.start_service()
        for scene in (check_native, authentication, timeline, pagination, attachments, revalidation,
                      archive_faults, limits, dependencies, timeout_and_busy, readonly, configuration):
            try:
                scene(lab, data)
            except Exception as exc:
                lab.check("scenario completes " + scene.__name__, False, exception=type(exc).__name__)
                (lab.artifacts / ("failure-" + scene.__name__ + ".txt")).write_text(lab.clean(traceback.format_exc()))
    except Exception as exc:
        lab.check("independent acceptance setup/completion", False, exception=type(exc).__name__)
        (lab.artifacts / "failure-fatal.txt").write_text(lab.clean(traceback.format_exc()))
    finally:
        lab.close()
    all_artifact_text = "\n".join(p.read_text(errors="replace") for p in lab.artifacts.rglob("*") if p.is_file())
    leaked = [secret for secret in lab.secrets if secret in all_artifact_text]
    lab.check("all generated acceptance artifacts omit random credentials and keys", not leaked)
    summary = dict(passed=sum(c["status"] == "PASS" for c in lab.checks), failed=sum(c["status"] == "FAIL" for c in lab.checks),
                   command="uv run --no-sync python tests/issue22_e2e.py", artifacts=str(lab.artifacts))
    write(lab.artifacts / "summary.json", summary)
    return int(bool(summary["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
