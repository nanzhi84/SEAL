"""Reproduce historical probe spelling comparisons from private research inputs."""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


def audit(root, b2):
    rows = json.loads((root / ("assigned-b2.json" if b2 else "assigned.json")).read_text())
    probes = json.loads((root / "probes.json").read_text())
    index = {(r["research_id"], r["role"]): r for r in probes}
    checks = []
    for row in rows:
        for role, key in [("entry", "entry_url"), ("candidate", "candidate_url")]:
            url = row.get(key, "").strip()
            if not url or (b2 and (row["id"], role) not in index):
                continue
            p = urlsplit(url)
            wire = (
                urlunsplit(
                    (
                        p.scheme,
                        p.netloc,
                        quote(p.path, safe="/%:@"),
                        quote(p.query, safe="=&%;+,:@/"),
                        "",
                    )
                )
                if b2
                else quote(url, safe=":/?=&%#")
            )
            check = {
                "research_id": row["id"],
                "role": role,
                "ascii_input": url.isascii(),
                "changed_by_historical_quote": wire != url,
            }
            if b2:
                actual = index[(row["id"], role)]["requested_url"]
                check.update(
                    probe_matches_reproduction=actual == wire,
                    input_sha256=hashlib.sha256(url.encode()).hexdigest(),
                    requested_sha256=hashlib.sha256(actual.encode()).hexdigest(),
                )
            checks.append(check)
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-root", type=Path, default=Path("work"))
    args = parser.parse_args()
    print(
        json.dumps(
            {
                name: audit(args.research_root / folder, b2)
                for name, folder, b2 in [
                    ("b2", "expanded-c-b2", True),
                    ("b1", "expanded-b1", False),
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
