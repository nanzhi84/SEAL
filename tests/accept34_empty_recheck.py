#!/usr/bin/env python3
"""An empty batch update cannot silently count as due-resource verification."""

import hashlib
import subprocess
import sys
import threading
import traceback
from http.server import ThreadingHTTPServer

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
        t.query("UPDATE seal.resource SET next_check=now()-interval '1 hour'")
        t.MODE["empty"] = True
        r, rc = t.cmd("recheck", t.config("empty-update"), "--no-progress")
        observations = t.query("SELECT * FROM seal.observation WHERE run_id=%s", (r.get("run_id"),))
        t.save("empty-recheck-observations.json", observations)
        t.save("empty-recheck-run.json", r)
        t.check(
            "empty updates do not replace five due-resource checks",
            len(observations) == 5 and all(o["outcome"] == "failed" for o in observations),
            {"returncode": rc, "observations": observations, "report": r.get("report")},
        )
        t.check(
            "empty update failures explicit",
            bool(r.get("report", {}).get("failures")) and rc != 0,
            r.get("report"),
        )
        t.check(
            "empty update preserves successful immutable versions",
            t.query("SELECT * FROM seal.version ORDER BY id") == before,
        )
    except Exception as exc:
        t.save("exception.json", {"exception": str(exc), "traceback": traceback.format_exc()})
        t.check("empty update harness completes", False, str(exc))
        traceback.print_exc()
    finally:
        t.save(
            "provenance.json",
            {
                "head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=t.ROOT, text=True
                ).strip(),
                "tests_start_sha256": t.TESTS_AT_START,
                "source_hashes": t.SOURCE_AT_START,
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
