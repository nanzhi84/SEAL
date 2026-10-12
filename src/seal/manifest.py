"""Immutable run evidence complements the PostgreSQL ledger; no output assembler."""

from .core import Objects, SealError
from .db import one

MANIFEST_CONTRACT = "seal.run_manifest.v2"


def terminal_report(c, source, run, status, reason):
    from .discovery import finish_discovery
    from .discovery_report import network_counts, summarize_discovery
    from .recheck import scope_details

    errors = set(run["errors"])
    if reason:
        errors.add(reason)
    finish_discovery(c, run, run["report"].get("finish_reason", reason))
    discovery = summarize_discovery(c, run)
    errors.update(discovery["unknown_coverage_reasons"])
    outputs = c.execute(
        """SELECT document_id,document_version_id,snapshot_id,observation_id FROM seal.document_emission
        WHERE run_id=%s AND attempt_epoch=%s ORDER BY document_id,created_at,id""",
        (run["id"], run["attempt_epoch"]),
    ).fetchall()
    report = dict(
        run["report"],
        errors=sorted(errors),
        document_outputs=outputs,
        document_counts={
            "document_count": len({r["document_id"] for r in outputs}),
            "emission_count": len(outputs),
        },
        discovery=discovery,
        resource_counts=network_counts(c, run),
        scope_evidence={
            **scope_details(run),
            "known_queue_exhausted": run["report"].get("finish_reason") == "finished",
            "site_population": "unknown",
            "unknown_coverage": True,
        },
        quality_status="not_evaluated",
    )
    binding = one(c, "SELECT recipe_version FROM seal.binding WHERE id=%s", (run["binding_id"],))
    manifest = {
        "contract": MANIFEST_CONTRACT,
        "source_id": source["id"],
        "binding_id": run["binding_id"],
        "recipe_version": binding["recipe_version"],
        "run_id": run["id"],
        "mode": run["mode"],
        "run_status": status,
        "attempt_epoch": run["attempt_epoch"],
        "inputs": run["inputs"],
        "report": report,
    }
    try:
        report["manifest_id"] = Objects().put_json(manifest)
        Objects().get(report["manifest_id"])
    except (SealError, OSError, TypeError, ValueError):
        report["manifest_id"] = None
        report["errors"] = sorted(errors | {"manifest_archive_failed"})
    return report
