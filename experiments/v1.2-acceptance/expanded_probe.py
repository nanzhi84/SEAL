"""Reproduce bounded public diagnostics for selected original ABC sources.

Reuse the established read-only probe's known robots restrictions, no-proxy GET,
per-host pacing, response limit and sensitive-body exclusion. This is mechanism
reconnaissance, never a substitute for Source/Binding/Run business acceptance.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/source-accessibility"))
from probe import Probe, now, save  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--urls-file",
        type=Path,
        help="Reviewed per-Source public URL list; never discover bypasses",
    )
    parser.add_argument("--no-redirect", action="store_true", help="Stop at the first redirect")
    parser.add_argument("--no-retain-body", action="store_true", help="Only record safe metadata")
    args = parser.parse_args()
    records = json.loads(
        (ROOT / "experiments/source-accessibility/results/classification/entries.json").read_text()
    )
    selected = {r["id"]: r for r in records if r["id"] in args.source and r["class"] in "ABC"}
    if set(selected) != set(args.source):
        raise ValueError("Every selected ID must be an original ABC entry")
    explicit = json.loads(args.urls_file.read_text()) if args.urls_file else None
    if explicit is not None and (
        not isinstance(explicit, dict)
        or set(explicit) != set(selected)
        or any(
            not isinstance(urls, list)
            or not 1 <= len(urls) <= 3
            or any(
                not isinstance(url, str) or not url.startswith(("http://", "https://"))
                for url in urls
            )
            for urls in explicit.values()
        )
    ):
        raise ValueError("Reviewed URL list must exactly match the selected Sources (1..3 each)")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "bodies").mkdir()
    probe = Probe(args.output)
    results = []
    for rid, row in selected.items():
        urls = list(
            dict.fromkeys(
                u.strip()
                for u in (row["entry_url"], row["sample_url"], row["candidate_url"])
                if u.strip()
            )
        )
        if explicit is not None:
            urls = explicit[rid]
        observed = []
        for url in urls[:3]:
            result = probe.request(
                url,
                "expanded-diagnostic",
                max_redirects=0 if args.no_redirect else 4,
                retain_body=not args.no_retain_body,
            )
            observed.append(result)
            if result.get("status") in (401, 403, 429) or result.get("outcome") in (
                "ROBOTS_DENIED",
                "ACCESS_RESTRICTED",
                "SENSITIVE_BODY_NOT_RETAINED",
            ):
                break
        results.append({"research_id": rid, "class": row["class"], "observations": observed})
        save(args.output / "results.json", results)
        print(
            json.dumps({"research_id": rid, "outcomes": [r["outcome"] for r in observed]}),
            flush=True,
        )
    files = {
        str(p.relative_to(args.output)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in args.output.rglob("*")
        if p.is_file()
    }
    save(
        args.output / "manifest.json",
        {
            "artifact_version": 1,
            "scope": "Bounded mechanism diagnostics; no business acceptance claim",
            "completed_at": now(),
            "selected_ids": sorted(selected),
            "command": "uv run --frozen python experiments/v1.2-acceptance/expanded_probe.py "
            + " ".join("--source " + rid for rid in sorted(selected))
            + (" --urls-file " + str(args.urls_file) if args.urls_file else "")
            + (" --no-redirect" if args.no_redirect else "")
            + (" --no-retain-body" if args.no_retain_body else "")
            + " --output <new-directory>",
            "protocol": {
                "max_urls_per_source": 3,
                "method": "GET",
                "max_redirects": 0 if args.no_redirect else 4,
                "retain_body": not args.no_retain_body,
                "timeout_seconds": 12,
                "max_response_bytes": 5242880,
                "per_host_delay_seconds": 1.5,
                "cookies": False,
                "environment_proxy": False,
                "known_robots_denials": "preserved",
                "sensitive_bodies": "not retained",
            },
            "files": files,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
