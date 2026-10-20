#!/usr/bin/env python3
"""Conflict/idempotency acceptance through duplicate source targets in real CLI."""

import hashlib
import subprocess
import sys
import threading
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path

import accept34_e2e as t


def main():
    t.MODE["inline"] = True
    t.dbsetup()
    server = ThreadingHTTPServer(("127.0.0.1", 0), t.Origin)
    t.BASE = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        t.cmd("init", expected=[0])
        t.cmd("collect", t.config("baseline"), "--no-progress", expected=[0])
        before = t.query("SELECT * FROM seal.version ORDER BY id")
        t.save("baseline-versions.json", before)
        t.MODE["duplicate"] = "same"
        r, rc = t.cmd("collect", t.config("identical-replay"), "--no-progress")
        t.save(
            "identical-replay-observations.json",
            t.query("SELECT * FROM seal.observation WHERE run_id=%s", (r.get("run_id"),)),
        )
        t.check("identical duplicate target completes", rc == 0, r.get("report"))
        t.check(
            "identical duplicate yields five observations",
            t.query(
                "SELECT count(*) AS n FROM seal.observation WHERE run_id=%s", (r.get("run_id"),)
            )[0]["n"]
            == 5,
        )
        t.check(
            "identical duplicate creates no conflict",
            not t.query("SELECT * FROM seal.delivery_conflict"),
        )
        t.check(
            "identical duplicate preserves immutable versions",
            t.query("SELECT * FROM seal.version ORDER BY id") == before,
        )
        t.MODE["duplicate"] = "changed"
        r, rc = t.cmd("collect", t.config("conflicting-replay"), "--no-progress")
        conflicts = t.query("SELECT * FROM seal.delivery_conflict")
        t.save("conflicts.json", conflicts)
        t.save(
            "conflict-run.json", t.query("SELECT * FROM seal.run WHERE id=%s", (r.get("run_id"),))
        )
        t.check(
            "same delivery key different input is explicit failure",
            rc != 0 and bool(conflicts),
            {"returncode": rc, "conflicts": conflicts, "report": r.get("report")},
        )
        t.check(
            "conflict does not mutate successful versions",
            t.query("SELECT * FROM seal.version ORDER BY id") == before,
        )
        t.check(
            "conflict reason exposed",
            any(c["reason"] == "delivery_key_conflict" for c in conflicts),
            conflicts,
        )
        sid = t.snapshot("after-conflict")
        t.api(sid, "after-conflict")
    except Exception as exc:
        t.save("exception.json", {"exception": str(exc), "traceback": traceback.format_exc()})
        t.check("conflict harness completes", False, str(exc))
        traceback.print_exc()
    finally:
        t.save(
            "provenance.json",
            {
                "head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=t.ROOT, text=True
                ).strip(),
                "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "support_sha256": hashlib.sha256(
                    (t.ROOT / "tests/accept34_e2e.py").read_bytes()
                ).hexdigest(),
                "source_hashes": t.SOURCE_AT_START,
                "tests_start_sha256": t.TESTS_AT_START,
                "source_end_hashes": {
                    str(p.relative_to(t.ROOT / "src")): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((t.ROOT / "src").rglob("*"))
                    if p.is_file() and p.suffix in (".py", ".sql")
                },
                "artifact": str(t.ART),
            },
        )
        t.save("assertions.json", t.ASSERTIONS)
        print("ARTIFACT", t.ART, flush=True)


if __name__ == "__main__":
    main()
    sys.exit(any(not a["passed"] for a in t.ASSERTIONS))
