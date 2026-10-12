"""Document-only external behavior acceptance, specified before implementation."""

import argparse
import hashlib
import json
import sys
import traceback

from harness import Harness


def journey(h):
    help_result = h.cli("--help")
    help_text = help_result["stdout"]
    h.check("four_commands", "{init,collect,recheck,worker}" in help_text)
    config = h.config(
        "all", ["/documents/root", "/documents/image.png", "/documents/compressed.txt"]
    )
    h.check(
        "uninitialized_collect", h.cli("collect", config, exit=2)["error"], "database_init_required"
    )
    h.cli("init")
    h.cli("init")
    for command in (
        "run",
        "replay",
        "inspect",
        "export",
        "source",
        "recipe",
        "binding",
        "retry",
        "recover",
        "finish",
        "pause",
        "schedule",
        "db",
    ):
        h.cli(command, exit=2)
    first = h.cli("collect", config)
    h.check("collect_complete", first["status"], "complete")
    docs = h.sql(
        """SELECT d.url,v.*,e.observation_id AS actual_observation FROM seal.document d
        JOIN seal.document_version v ON v.id=d.latest_document_version
        JOIN seal.document_emission e ON e.document_version_id=v.id WHERE e.run_id=%s""",
        (first["run_id"],),
    )
    h.capture("documents", docs)
    h.check(
        "nine_formats",
        sorted({d["source_format"] for d in docs}),
        ["csv", "docx", "html", "image", "json", "pdf", "txt", "xls", "xlsx"],
    )
    h.check(
        "five_nodes",
        sorted({n["type"] for d in docs for n in d["content"]["children"]}),
        ["article", "document_ref", "image", "json", "table"],
    )
    known = {d["url"]: d for d in docs}
    h.check(
        "decoded_archive_content",
        known[h.site.url + "/documents/compressed.txt"]["content"]["children"][0]["text"],
        "Compressed public text",
    )
    h.check(
        "pdf_text",
        known[h.site.url + "/documents/report.pdf"]["content"]["children"][0]["text"],
        "Synthetic PDF notice",
    )
    h.check(
        "json_full_envelope",
        known[h.site.url + "/documents/data.json"]["content"]["children"],
        [
            {
                "type": "json",
                "value": {"year": 2026, "authority": None, "tags": ["policy", "science"]},
            }
        ],
    )
    h.check(
        "csv_native_table",
        known[h.site.url + "/documents/public.csv"]["content"]["children"][0]["rows"],
        [
            ["name", "amount", "comment"],
            ["Public, entity", "42", 'quoted "value"'],
            ["Empty", "", ""],
        ],
    )
    versions = {d["id"] for d in docs}
    again = h.cli("collect", config)
    h.check("binding_reused", again["binding_id"], first["binding_id"])
    h.check(
        "versions_reused",
        {d["id"] for d in h.sql("SELECT id FROM seal.document_version")},
        versions,
    )
    unchanged = h.cli("recheck", config)
    h.check("recheck_complete", unchanged["status"], "complete")
    h.check("recheck_unchanged", unchanged["summary"]["counts"]["unchanged"], len(known))
    h.site.resources["/documents/plain.txt"] = ("text/plain", b"Updated public text")
    updated = h.cli("recheck", config)
    h.check("one_updated", updated["summary"]["counts"]["updated"], 1)
    h.check(
        "one_new_version", len(h.sql("SELECT id FROM seal.document_version")), len(versions) + 1
    )
    h.capture("recheck", {"unchanged": unchanged, "updated": updated})
    # The oracle reads archive bytes independently of Runtime helpers.
    observed = h.sql("SELECT * FROM seal.fetch_observation WHERE snapshot_id IS NOT NULL")
    hashes = []
    for row in observed:
        key = row["body_hash"]
        body = (h.work / "archive/objects" / key[:2] / key[2:]).read_bytes()
        h.check("archive_hash_" + row["id"], hashlib.sha256(body).hexdigest(), key)
        snapshot_key = row["snapshot_id"]
        wire = (h.work / "archive/objects" / snapshot_key[:2] / snapshot_key[2:]).read_bytes()
        h.check("snapshot_hash_" + row["id"], hashlib.sha256(wire).hexdigest(), snapshot_key)
        h.check("snapshot_body_" + row["id"], json.loads(wire)["body_hash"], key)
        hashes.append({"snapshot_id": snapshot_key, "body_hash": key, "size": len(body)})
    h.capture("archive-oracle", hashes)
    failures = h.config(
        "failures",
        [
            "/documents/empty.txt",
            "/documents/scanned.pdf",
            "/documents/bad.pdf",
            "/documents/bad.docx",
            "/documents/bad.xlsx",
            "/documents/bad.json",
            "/documents/unsupported.bin",
            "/documents/access-denied",
        ],
    )
    failed = h.cli("collect", failures, exit=1)
    h.check("parse_failures", failed["status"], "failed")
    errors = h.sql(
        "SELECT url,processing_stage,processing_error,snapshot_id FROM seal.fetch_observation WHERE run_id=%s",
        (failed["run_id"],),
    )
    h.capture("parse-errors", errors)
    h.check("failed_inputs_archived", all(r["snapshot_id"] for r in errors))
    h.check(
        "parse_error_reasons",
        {r["processing_error"] for r in errors if r["processing_error"]}
        >= {
            "empty_document",
            "pdf_text_layer_required",
            "pdf_parse_failed",
            "docx_parse_failed",
            "xlsx_parse_failed",
            "json_parse_failed",
            "unsupported_content_type",
            "fallback_error_page",
        },
    )
    h.check(
        "discovery_after_parse_failure",
        bool(
            h.sql(
                "SELECT id FROM seal.document WHERE source_id='failures' AND url=%s AND EXISTS(SELECT 1 FROM seal.document_version v WHERE v.document_id=seal.document.id)",
                (h.site.url + "/documents/plain.txt",),
            )
        ),
    )
    queued = h.cli("collect", config, "--enqueue")
    h.check("enqueue_pending", queued["status"], "pending")
    h.cli("worker", "--once")
    h.check(
        "worker_consumed",
        h.sql("SELECT status FROM seal.run WHERE id=%s", (queued["run_id"],))[0]["status"],
        "complete",
    )
    with __import__("psycopg").connect(h.env["SEAL_DATABASE_URL"]) as c:
        with c.cursor().copy(
            "COPY (SELECT id,source_format,content FROM seal.document_version ORDER BY id) TO STDOUT"
        ) as copy:
            body = b"".join(bytes(chunk) for chunk in copy)
        (h.output / "versions.copy").write_bytes(body)
    h.check(
        "copy_has_documents",
        len(body.splitlines()),
        len(h.sql("SELECT id FROM seal.document_version")),
    )
    h.capture(
        "lineage",
        h.sql("""SELECT e.*,v.revision_id,r.body_hash FROM seal.document_emission e
        JOIN seal.document_version v ON v.id=e.document_version_id
        JOIN seal.revision r ON r.id=v.revision_id"""),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    h = Harness(args.name)
    error = None
    try:
        h.start()
        journey(h)
        from adapter_acceptance import exercise as adapters

        adapters(h)
        from runtime_acceptance import exercise as runtime

        runtime(h)
        from migration_acceptance import exercise

        exercise(h)
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
        traceback.print_exc()
    finally:
        h.save(error)
        h.close()
    print(
        json.dumps(
            {
                "status": "FAIL" if error else "PASS",
                "assertions": len(h.checks),
                "artifacts": str(h.output),
                "error": error,
            }
        )
    )
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
