"""External transport/fencing contracts through real isolated CLI collection.

Boundaries: each real redirect/retry has its own immutable Acquisition; scope and
robots prevent forbidden network requests; byte/document budgets are finite;
waiting on external input does not keep a PostgreSQL transaction open.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from issue31_common import collect, current_rows, failure_evidence
from issue31_support import Fixture, base_source, html_page


def test_retry_facts(lab):
    """A 503 then 200 creates two linked immutable Acquisition facts."""
    fx = Fixture()
    try:
        body = html_page("Retry Success", "<p>The successful retry contains the public document body.</p>").encode()
        fx.routes["/retry"] = [(503, "text/html", b"temporary upstream failure"), (200, "text/html", body)]
        cfg = base_source("dd-058", fx, ["/retry"], fetch={"retries": 1})
        result, _, _ = collect(lab, "dd-058", cfg, label="retry-facts")
        lab.check("retry collection succeeds", result.returncode == 0, result.stderr[-800:])
        acq = lab.q("SELECT id,state,http_status,parent_acquisition_id,parent_relation,request_key FROM seal.acquisition WHERE request_url=%s ORDER BY started_at", (fx.base + "/retry",))
        lab.save("evidence/transport-retry.json", acq)
        lab.check("exactly two real requests", fx.hits.get("/retry") == 2 and len(acq) == 2, {"hits": fx.hits, "acquisitions": acq})
        lab.check("retry linked to prior request", acq[1]["parent_acquisition_id"] == acq[0]["id"] and acq[1]["parent_relation"] == "retry", acq)
        lab.check("same request key, preserved 503 then 200", acq[0]["request_key"] == acq[1]["request_key"] and [a["http_status"] for a in acq] == [503, 200], acq)
        blocked = False
        try:
            lab.q("UPDATE seal.acquisition SET state='failed' WHERE id=%s RETURNING id", (acq[1]["id"],))
        except Exception:
            blocked = True
        lab.check("terminal Acquisition cannot be rewritten", blocked, acq[1]["id"])
        after = lab.q("SELECT state FROM seal.acquisition WHERE id=%s", (acq[1]["id"],))[0]["state"]
        lab.check("terminal fact remains archived", after == "archived", after)
    finally:
        fx.close()


def test_redirect_scope_facts(lab):
    """Redirect hops have lineage and out-of-scope links never reach the source."""
    fx = Fixture()
    try:
        fx.put("/allowed/start", b"", status=302, headers={"Location": "/allowed/document"})
        fx.put("/allowed/document", html_page("Redirect Public Document", "<p>Final document body.</p>", links=[("/outside/private", "Forbidden link")]))
        fx.put("/outside/private", html_page("Private", "Should never be fetched"))
        cfg = base_source("dd-058", fx, ["/allowed/start"], allowed_path_prefixes=["/allowed"])
        result, _, _ = collect(lab, "dd-058", cfg, label="redirect-scope")
        lab.check("redirect collection succeeds", result.returncode == 0, result.stderr[-800:])
        acq = lab.q("SELECT id,request_url,http_status,parent_acquisition_id,parent_relation FROM seal.acquisition WHERE source_id='dd-058' AND request_url LIKE %s ORDER BY started_at", (fx.base + "%",))
        lab.save("evidence/transport-redirect.json", {"requests": fx.requests, "acquisitions": acq})
        first = next(a for a in acq if a["request_url"].endswith("/allowed/start"))
        last = next(a for a in acq if a["request_url"].endswith("/allowed/document"))
        lab.check("redirect has independent linked fact", first["id"] != last["id"] and last["parent_acquisition_id"] == first["id"] and last["parent_relation"] == "redirect", acq)
        lab.check("out-of-scope request prevented", "/outside/private" not in fx.requests, fx.requests)
    finally:
        fx.close()


def test_robots_prevents_request(lab):
    """Robots disallow archives policy evidence and prevents the content request."""
    fx = Fixture()
    try:
        fx.robots("User-agent: *\nDisallow: /private\n")
        fx.put("/private", html_page("Forbidden", "Never download this document."))
        cfg = base_source("dd-058", fx, ["/private"], robots=True)
        result, receipt, _ = collect(lab, "dd-058", cfg, label="robots-scope")
        lab.save("evidence/transport-robots.json", {"requests": fx.requests, "receipt": receipt})
        lab.check("robots policy requested", "/robots.txt" in fx.requests, fx.requests)
        lab.check("disallowed content never requested", "/private" not in fx.requests, fx.requests)
        policy = lab.q("SELECT role,state FROM seal.acquisition WHERE request_url=%s", (fx.base + "/robots.txt",))
        lab.check("robots policy archived separately", policy and all(a["role"] == "policy" and a["state"] == "archived" for a in policy), policy)
        rows = [r for r in current_rows(lab, "dd-058") if r["public_url"] == fx.base + "/private"]
        lab.check("robots-blocked document not delivered", not rows, rows)
    finally:
        fx.close()


def test_byte_budget_no_partial_archive(lab):
    """An oversized response fails explicitly and never claims partial bytes as original."""
    fx = Fixture()
    try:
        fx.put("/oversize.txt", "Public text " * 3000, "text/plain")
        cfg = base_source("dd-058", fx, ["/oversize.txt"], budget={"response_bytes": 1024})
        _, receipt, _ = collect(lab, "dd-058", cfg, label="byte-budget")
        acq = lab.q("SELECT state,archive_ref,error,outcome FROM seal.acquisition WHERE request_url=%s", (fx.base + "/oversize.txt",))
        lab.save("evidence/transport-byte-budget.json", {"acquisitions": acq, "receipt": receipt})
        lab.check("oversize acquisition explicitly failed", acq and all(a["state"] == "failed" and a["error"] for a in acq), acq)
        lab.check("no full-original archive claim for partial response", all(a["archive_ref"] is None for a in acq), acq)
        rows = [r for r in current_rows(lab, "dd-058") if r["public_url"] == fx.base + "/oversize.txt"]
        lab.check("oversize document not delivered", not rows, rows)
    finally:
        fx.close()


def test_document_budget(lab):
    """The document cap bounds accepted targets; excess targets retain skipped facts."""
    fx = Fixture()
    try:
        for number in range(3):
            fx.put(f"/detail/{number}", html_page(f"Budget Document {number}", "<p>Public document body with explicit obligations.</p>"))
        fx.put("/listing", html_page("Listing", links=[(f"/detail/{n}", str(n)) for n in range(3)]))
        cfg = base_source("dd-058", fx, ["/listing"], budget={"documents": 1},
                          documents={"detail_url_patterns": [r".*/detail/.*"]})
        _, receipt, _ = collect(lab, "dd-058", cfg, label="document-budget")
        rows = [r for r in current_rows(lab, "dd-058") if r["public_url"].startswith(fx.base)]
        targets = lab.q("SELECT t.state,t.error FROM seal.task t JOIN seal.run r ON r.id=t.run_id WHERE r.source_id='dd-058' AND t.kind='target' AND t.payload::text LIKE %s", ("%" + fx.base + "%",))
        skipped = [t for t in targets if t["state"] == "skipped" and t["error"] == "document_budget_exceeded"]
        accepted = len(targets) - len(skipped)
        evidence = {"receipt": receipt, "rows": len(rows), "registered_targets": len(targets),
                    "accepted_targets": accepted, "budget_skipped_targets": len(skipped), "targets": targets}
        lab.save("evidence/transport-document-budget.json", evidence)
        lab.check("one-document cap respected", len(rows) <= 1 and accepted <= 1, evidence)
    finally:
        fx.close()


def test_external_wait_has_no_long_transaction(lab):
    """A pending external HTTP request holds no PostgreSQL transaction across the wait."""
    fx = Fixture()
    gate = threading.Event()
    fx.put("/wait.txt", "Public waiting document body with clear content.", "text/plain")
    fx.hang["/wait.txt"] = gate
    child = None
    try:
        cfg = base_source("dd-058", fx, ["/wait.txt"])
        path = lab.source_file("dd-058", cfg)
        child = lab.popen("collect", path, "--no-progress")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not fx.hits.get("/wait.txt") and child.poll() is None:
            time.sleep(.1)
        lab.check("real request is waiting", fx.hits.get("/wait.txt"), Path(child.logfile).read_text()[-800:])
        time.sleep(2)
        transactions = lab.q("SELECT pid,state,application_name,wait_event_type,wait_event,backend_xmin,extract(epoch FROM clock_timestamp()-xact_start) AS transaction_seconds,left(query,800) AS last_query FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND xact_start < clock_timestamp()-interval '1 second'")
        lab.save("evidence/transport-external-transactions.json", transactions)
        lab.check("no transaction spans external wait", not transactions, transactions)
    finally:
        gate.set()
        if child:
            child.wait(60)
        fx.close()
