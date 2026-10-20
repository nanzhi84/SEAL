#!/usr/bin/env python3
"""Fresh observable dependency matrix. Independent real CLI/HTTP/PG/S3 cases."""

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import subprocess
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import accept34_e2e as t
import yaml

STATE = {"phase": 0}
CASE = ""
TEXT = "All designated authorities shall follow the public procedures and legal duties established by this regulation."


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        t.REQUESTS.append({"path": self.path, "phase": STATE["phase"]})
        t.save("requests.json", t.REQUESTS)
        status = 200
        mime = "text/html; charset=utf-8"
        headers = {}
        phase = STATE["phase"]
        if CASE == "charset":
            body = f"<html><h1>Character declaration law</h1><p>{TEXT} The public café shall comply.</p></html>".encode(
                "utf-8"
            )
            mime = "text/html; charset=" + ("utf-8" if phase == 0 else "windows-1252")
        elif CASE == "base":
            if self.path == "/entry":
                status = 302
                headers["Location"] = "/base-" + ("a" if phase == 0 else "b") + "/document"
                body = b""
            else:
                body = f'<html><h1>Base dependent law</h1><p>{TEXT} See <a href="reference.html">the official reference</a>.</p></html>'.encode()
        elif CASE == "outer-date":
            date = "2026-10-01" if phase < 2 else "2026-10-02"
            noise = "nav-one"
            body = f'<html><h1>External effective date law</h1><span id="date">{date}</span><span id="noise">{noise}</span><article id="body"><p>{TEXT}</p></article></html>'.encode()
        else:
            if self.path.endswith(".pdf"):
                mime = "application/pdf"
                body = t.pdf(1 if self.path == "/a.pdf" else 2)
            else:
                links = [("/a.pdf", "Part 1"), ("/b.pdf", "Part 2")]
                if phase == 1:
                    links.reverse()
                if phase == 2:
                    links = [("/a.pdf", "Part 2"), ("/b.pdf", "Part 1")]
                body = (
                    "<html><h1>Continuous volume law</h1>"
                    + "".join(f'<a href="{u}">{label}</a>' for u, label in links)
                    + "</html>"
                ).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)


def current():
    return t.query(
        "SELECT v.* FROM seal.resource r JOIN seal.version v ON v.id=r.current_version_id"
    )[0]


def collect(source, label):
    r, rc = t.cmd("collect", source, "--no-progress")
    t.check(label + " real CLI succeeds", rc == 0, r.get("report"))
    t.save(label + "-run.json", r)
    return r, current()


def markdown_hash(version):
    return t.query(
        "SELECT markdown_hash FROM seal.delivery_row WHERE version_id=%s ORDER BY row_number",
        (version["id"],),
    )[0]["markdown_hash"]


def success_bodies(run):
    return t.query(
        "SELECT body_hash,actual_url,charset FROM seal.acquisition WHERE run_id=%s AND http_status=200 ORDER BY acquired_at",
        (run["run_id"],),
    )


def final_provenance():
    return {
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
    }


def child(case):
    global CASE
    CASE = case
    t.dbsetup()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    t.BASE = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        t.cmd("init", expected=[0])
        source = Path(t.config(case))
        cfg = yaml.safe_load(source.read_text())
        cfg.update(
            adapter="web",
            adapter_options={},
            entry_urls=[
                t.BASE
                + ("/entry" if case == "base" else "/law" if case == "volumes" else "/document")
            ],
        )
        cfg["fetch"]["max_depth"] = 0
        if case == "outer-date":
            cfg["documents"] = {
                "content_xpath": "//article[@id='body']",
                "fields": {
                    "published_at": {"xpath": "//span[@id='date']", "date_format": "%Y-%m-%d"}
                },
            }
        if case == "volumes":
            cfg["documents"] = {
                "detail_url_patterns": ["/law$"],
                "volume_pattern": r"(?i)part\s+([12])",
                "detail_body": "auto",
            }
        source.write_text(yaml.safe_dump(cfg, allow_unicode=True))
        t.save("source-config.json", cfg)
        first, v1 = collect(str(source), "baseline")
        before_versions = t.query("SELECT * FROM seal.version ORDER BY id")
        before_obs = t.query("SELECT * FROM seal.observation ORDER BY id")
        t.save("baseline-versions.json", before_versions)
        t.save("baseline-observations.json", before_obs)
        a = t.snapshot("A")
        proof_a = t.api(a, "A")
        a_rows = proof_a["items"]
        snapshot_a = t.query("SELECT * FROM seal.snapshot WHERE id=%s", (a,))
        coverage_a = t.query("SELECT * FROM seal.snapshot_source WHERE snapshot_id=%s", (a,))
        t.check("one stable resource at baseline", len(t.query("SELECT * FROM seal.resource")) == 1)
        if case == "volumes":
            STATE["phase"] = 1
            control, control_v = collect(str(source), "irrelevant-control")
            t.check(
                "unselected noise or DOM link order does not create Version",
                control_v["id"] == v1["id"],
                {"old": v1["id"], "new": control_v["id"]},
            )
            t.check(
                "control is an unchanged delivery",
                control.get("report", {}).get("observations", {}).get("unchanged") == 1,
                control.get("report"),
            )
            STATE["phase"] = 2
        else:
            STATE["phase"] = 2 if case == "outer-date" else 1
        changed, v2 = collect(str(source), "changed-dependency")
        t.save("final-versions.json", t.query("SELECT * FROM seal.version ORDER BY id"))
        t.save(
            "final-acquisitions.json",
            t.query("SELECT * FROM seal.acquisition ORDER BY acquired_at"),
        )
        t.check(
            "dependency change creates exactly second Version",
            len(t.query("SELECT * FROM seal.version")) == 2 and v2["sequence"] == 2,
            {"v1": v1, "v2": v2},
        )
        t.check("stable Resource identity retained", v1["resource_id"] == v2["resource_id"])
        t.check("same parser retained", v1["parser_fingerprint"] == v2["parser_fingerprint"])
        t.check(
            "actual dependency changes input fingerprint",
            v1["input_fingerprint"] != v2["input_fingerprint"],
        )
        t.check("change is explained as source_changed", v2["change_reason"] == "source_changed")
        first_bodies = success_bodies(first)
        last_bodies = success_bodies(changed)
        t.save("entity-comparison.json", {"before": first_bodies, "after": last_bodies})
        if case in ("charset", "base"):
            t.check(
                "HTTP entity bytes fixed",
                len(first_bodies) == len(last_bodies) == 1
                and first_bodies[0]["body_hash"] == last_bodies[0]["body_hash"],
            )
            if case == "charset":
                t.check(
                    "charset declarations differ",
                    first_bodies[0]["charset"] != last_bodies[0]["charset"],
                )
                t.check("decoding visibly changes Markdown", markdown_hash(v1) != markdown_hash(v2))
            else:
                t.check(
                    "actual base URLs differ",
                    first_bodies[0]["actual_url"] != last_bodies[0]["actual_url"],
                )
                t.check(
                    "identity stays entry request URL",
                    t.query("SELECT external_key FROM seal.resource")[0]["external_key"]
                    == t.BASE + "/entry",
                )
        if case == "outer-date":
            t.check(
                "selected date changes while Markdown stays fixed",
                markdown_hash(v1) == markdown_hash(v2),
            )
        b = t.snapshot("B")
        proof_b = t.api(b, "B")
        b_rows = proof_b["items"]
        if case == "outer-date":
            t.check(
                "actual external date appears in 16-column projection",
                a_rows[0]["发布日期"] == "2026-10-01" and b_rows[0]["发布日期"] == "2026-10-02",
                {"before": a_rows, "after": b_rows},
            )
        if case == "volumes":

            def mapping(rr):
                return {int(r["文件顺序"]): r["original_hash"] for r in rr}

            am, bm = mapping(a_rows), mapping(b_rows)
            t.check(
                "declared continuous volume order reverses original mapping",
                set(am) == set(bm) == {1, 2}
                and am[1] == bm[2]
                and am[2] == bm[1]
                and am[1] != am[2],
                {"before": am, "after": bm},
            )
            apdf = sorted(x["body_hash"] for x in first_bodies if x["actual_url"].endswith(".pdf"))
            bpdf = sorted(x["body_hash"] for x in last_bodies if x["actual_url"].endswith(".pdf"))
            t.check("both original PDF bytes unchanged", apdf == bpdf and len(apdf) == 2)
        t.check(
            "prior Version rows immutable",
            t.query(
                "SELECT * FROM seal.version WHERE id=ANY(%s) ORDER BY id",
                ([v["id"] for v in before_versions],),
            )
            == before_versions,
        )
        t.check(
            "prior Observation rows immutable",
            t.query(
                "SELECT * FROM seal.observation WHERE id=ANY(%s) ORDER BY id",
                ([o["id"] for o in before_obs],),
            )
            == before_obs,
        )
        t.check(
            "Snapshot A immutable database record and coverage",
            t.query("SELECT * FROM seal.snapshot WHERE id=%s", (a,)) == snapshot_a
            and t.query("SELECT * FROM seal.snapshot_source WHERE snapshot_id=%s", (a,))
            == coverage_a,
        )
        t.cmd("snapshot", "publish", a, expected=[0])
        again = t.api(a, "A-after-B")
        t.check("Snapshot A API records and file bytes fixed", again == proof_a)
        t.cmd("snapshot", "publish", b, expected=[0])
    except Exception as exc:
        t.save("exception.json", {"exception": str(exc), "traceback": traceback.format_exc()})
        t.check("matrix case completes", False, str(exc))
        traceback.print_exc()
    finally:
        t.save("provenance.json", final_provenance())
        t.save("assertions.json", t.ASSERTIONS)
        print("ARTIFACT", t.ART, flush=True)
    return any(not a["passed"] for a in t.ASSERTIONS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case", choices=["all", "charset", "base", "outer-date", "volumes"], default="all"
    )
    args = parser.parse_args()
    if args.case != "all":
        return child(args.case)

    def run(case):
        p = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--case", case],
            cwd=t.ROOT,
            env=dict(t.ENV),
            capture_output=True,
            text=True,
            timeout=120,
        )
        (t.ART / (case + ".log")).write_text(p.stdout + p.stderr)
        path = Path(
            next(
                line[9:] for line in reversed(p.stdout.splitlines()) if line.startswith("ARTIFACT ")
            )
        )
        assertions = json.loads((path / "assertions.json").read_text())
        row = {
            "case": case,
            "returncode": p.returncode,
            "artifact": str(path),
            "assertions": len(assertions),
            "failed": [a for a in assertions if not a["passed"]],
        }
        print(json.dumps({k: v for k, v in row.items() if k != "failed"}), flush=True)
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["charset", "base", "outer-date", "volumes"]))
    report = {
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=t.ROOT, text=True
        ).strip(),
        "cases": results,
        "total_assertions": sum(r["assertions"] for r in results),
        "failures": sum(len(r["failed"]) for r in results),
        "source_frozen": t.SOURCE_AT_START == final_provenance()["source_end_hashes"],
        "provenance": final_provenance(),
    }
    t.save("matrix-report.json", report)
    print("MATRIX_ARTIFACT", t.ART, flush=True)
    return any(r["returncode"] or r["failed"] for r in results) or not report["source_frozen"]


if __name__ == "__main__":
    sys.exit(main())
