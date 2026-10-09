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
    args = parser.parse_args()
    records = json.loads(
        (ROOT / "experiments/source-accessibility/results/classification/entries.json").read_text()
    )
    selected = {r["id"]: r for r in records if r["id"] in args.source and r["class"] in "ABC"}
    if set(selected) != set(args.source):
        raise ValueError("Every selected ID must be an original ABC entry")
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
        observed = [probe.request(url, "expanded-diagnostic") for url in urls[:3]]
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
            + " --output <new-directory>",
            "protocol": {
                "max_urls_per_source": 3,
                "method": "GET",
                "max_redirects": 4,
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
