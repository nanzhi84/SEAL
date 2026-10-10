"""Content addressed Runtime manifests; execution evidence, never a second ledger."""

from collections import Counter

from .core import Objects, SealError

MANIFEST_CONTRACT = "seal.run_manifest.v1"


def has_table(connection, name):
    return (
        connection.execute("SELECT to_regclass(%s) AS name", (name,)).fetchone()["name"] is not None
    )


def read_manifest(identity):
    try:
        value = Objects().json(identity)
    except (TypeError, ValueError):
        raise SealError("manifest_corrupt") from None
    if not isinstance(value, dict) or value.get("contract") != MANIFEST_CONTRACT:
        raise SealError("unsupported_manifest_contract")
    return value


def manifest_reference(run):
    """A missing historical manifest is different from a broken current artifact."""
    identity = run["report"].get("manifest_id")
    if not identity:
        return {"manifest_id": None, "availability": "not_recorded"}
    try:
        return {
            "manifest_id": identity,
            "availability": "available",
            "manifest": read_manifest(identity),
        }
    except SealError as exc:
        return {"manifest_id": identity, "availability": "unavailable", "reason": exc.code}


def write_manifest(source, binding, run, report, status, input_artifacts):
    # Freeze mapping from the Binding, rather than today's mutable Source configuration.
    manifest = {
        "contract": MANIFEST_CONTRACT,
        "research_ids": binding["config"].get("research_ids", []),
        "source_id": source["id"],
        "binding_id": binding["id"],
        "recipe_version": binding["recipe_version"],
        "run_id": run["id"],
        "mode": run["mode"],
        "namespace": run["namespace"],
        "attempt_epoch": run["attempt_epoch"],
        "source_generation": run.get("generation"),
        "run_seq": run.get("run_seq"),
        "scope": binding["config"]["scope"],
        "seeds": run["seeds"],
        "run_status": status,
        "quality_status": "not_evaluated",
        "errors": report["errors"],
        "coverage": report["scope_evidence"],
        "discovery": report["discovery"],
        "record_counts": report.get("record_counts", {}),
        "resource_counts": report.get("resource_counts", {}),
        "result_count": report.get("result_count", len(report["outputs"])),
        "artifacts": {
            "inputs": input_artifacts,
            "documents": report["outputs"],
            "records": report["record_outputs"],
        },
    }
    identity = Objects().put_json(manifest)
    # Report must never claim an artifact that cannot be read back.
    read_manifest(identity)
    return identity


def terminal_report(connection, source, run, status, reason):
    """Freeze terminal evidence without promoting results or changing business state.

    Used by early fencing, exhausted attempts and subprocess failures. Their input
    and attempt history remains available even when no normal finisher can run.
    """
    from .db import one

    report = dict(run["report"])
    errors = set(run["errors"]) | set(report.get("errors", []))
    if reason:
        errors.add(reason)
    artifacts = []
    for entry in run["inputs"]:
        try:
            snapshot = Objects().json(entry["snapshot_id"])
            Objects().get(snapshot["body_hash"])
            artifacts.append(
                dict(entry, body_hash=snapshot["body_hash"], body_size=snapshot["body_size"])
            )
        except SealError as exc:
            errors.add(exc.code)
            artifacts.append(dict(entry, availability="unavailable", reason=exc.code))
    discovery = {
        "contract": 1,
        "instrumented": False,
        "unknown_coverage": True,
        "unknown_coverage_reasons": ["discovery_not_recorded"],
    }
    if has_table(connection, "seal_discovery"):
        from .discovery import finish_discovery, summarize_discovery

        finish_discovery(connection, run, reason)
        discovery = summarize_discovery(connection, run)
    counts = dict(report.get("record_counts", {}))
    if has_table(connection, "seal_record"):
        observed = one(
            connection,
            """SELECT count(*) AS emission_count,count(DISTINCT record_id) AS observed_record_count
               FROM seal_record_emission WHERE run_id=%s AND attempt_epoch=%s""",
            (run["id"], run["attempt_epoch"]),
        )
        counts.update(observed)
        counts.setdefault("record_count", len(report.get("record_outputs", [])))
    from .discovery import network_counts

    fetch_counts = network_counts(connection, run)
    fetch_counts.update(
        input_count=len(run["inputs"]),
        input_roles=dict(Counter(entry["role"] for entry in run["inputs"])),
    )
    used_seeds = {(entry["logical_url"], entry["role"]) for entry in run["inputs"]}
    scope = dict(report.get("scope_evidence", {}))
    scope.update(
        seed_count=len(run["seeds"]),
        resolved_seed_count=sum((seed["url"], seed["role"]) in used_seeds for seed in run["seeds"]),
        business_population="unknown",
        unknown_coverage=True,
        unknown_coverage_reasons=sorted(
            errors | set(discovery.get("unknown_coverage_reasons", []))
        ),
        record_absence_semantics="unobserved_is_unknown_never_deleted",
    )
    report.update(
        terminal_reason=reason,
        outputs=report.get("outputs", []),
        record_outputs=report.get("record_outputs", []),
        record_counts=counts,
        discovery=discovery,
        resource_counts=fetch_counts,
        scope_evidence=scope,
        quality_status="not_evaluated",
        errors=sorted(errors),
    )
    binding = one(connection, "SELECT * FROM seal_binding WHERE id=%s", (run["binding_id"],))
    try:
        report["manifest_id"] = write_manifest(source, binding, run, report, status, artifacts)
    except (SealError, OSError, TypeError, ValueError) as exc:
        errors.add("manifest_archive_failed")
        report.update(
            manifest_id=None,
            manifest_error=exc.code if isinstance(exc, SealError) else "manifest_archive_failed",
            errors=sorted(errors),
        )
        scope["unknown_coverage_reasons"] = sorted(
            errors | set(discovery.get("unknown_coverage_reasons", []))
        )
    return report
