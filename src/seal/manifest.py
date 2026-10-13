"""Bounded run evidence: a handled queue never establishes site completeness."""


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
    return report
