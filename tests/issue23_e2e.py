"""Run: uv run --no-sync python tests/issue23_e2e.py

Requires Docker, local PostgreSQL17/MinIO/mc images and uv dependencies. All data
is synthetic, random isolated services. Tests never import product implementation.
"""
from __future__ import annotations

import traceback

from issue23_concurrency import atomic_view, in_flight, publication, worker_path
from issue23_contract import lifecycle, no_publication, permissions, tokens
from issue23_data import Data
from issue23_faults import archives, immutable, listing, readonly
from issue23_lab import Lab, write
from issue23_upgrade import upgrade


def main():
    lab = Lab()
    data = Data(lab)
    try:
        lab.start()
        # Empty snapshot is created before any source; init itself must create none.
        lab.check("new init creates neither snapshot nor publication",
                  lab.pointer() is None and lab.sql("SELECT count(*) AS n FROM seal.snapshot")[0]["n"] == 0)
        data.empty = lab.create("empty")
        data.prepare()
        lab.start_service()
        scenes = (no_publication, lifecycle, tokens, permissions, listing, immutable,
                  in_flight, atomic_view, worker_path, publication, archives, readonly, upgrade)
        for scene in scenes:
            try:
                scene(lab, data)
            except Exception as exc:
                lab.check("scenario completes " + scene.__name__, False, exception=type(exc).__name__)
                (lab.artifacts / ("failure-" + scene.__name__ + ".txt")).write_text(lab.clean(traceback.format_exc()))
        write(lab.artifacts / "publication-records.json", lab.audit())
        write(lab.artifacts / "final-sql-mappings.json", {name: lab.mapping(ident) for name, ident in lab.snapshots.items()})
    except Exception as exc:
        lab.check("acceptance setup completes", False, exception=type(exc).__name__)
        (lab.artifacts / "failure-fatal.txt").write_text(lab.clean(traceback.format_exc()))
    finally:
        lab.close()
    summary = lab.summary()
    print(summary, flush=True)
    return int(bool(summary["failed"]))


if __name__ == "__main__":
    raise SystemExit(main())
