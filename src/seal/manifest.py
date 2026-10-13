"""Bounded run evidence: a handled queue never establishes site completeness."""

from .core import Objects
from .db import one


def terminal_report(c, source, run, status, reason):
    from .progress import snapshot

    errors = sorted(set(run["errors"]) | ({reason} if reason else set()))
    report = dict(
        run["report"],
        errors=errors,
        counts=snapshot(c, run).counts,
        scope_evidence={
            "site_population": "unknown",
            "unknown_coverage": True,
            "absence_semantics": "unobserved_is_unknown_never_deleted",
        },
        quality_status="not_evaluated",
    )
    config_hash = one(c, "SELECT config_hash FROM seal.run WHERE id=%s", (run["id"],))[
        "config_hash"
    ]
    manifest = {
        "contract": "seal.run_manifest.v3",
        "source_id": source["id"],
        "run_id": run["id"],
        "config_hash": config_hash,
        "mode": run["mode"],
        "status": status,
        "report": report,
    }
    report["manifest_id"] = Objects().put_json(manifest)
    return report
