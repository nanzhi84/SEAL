"""One stable record per map row; deduplication never drops the user's denominator."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
MAP = ROOT / "experiments" / "due-diligence" / "inputs" / "source-map.md"
SENSITIVE = re.compile(r"token|password|secret|session|auth|api.?key|signature", re.I)
TRACKING = re.compile(r"^(?:utm_|qhclickid|saasdianping|addrid)", re.I)


def clean_url(url, fragment=False):
    p = urlsplit(url)
    path = re.sub(r";jsessionid=[^/?;#]*", "", p.path, flags=re.I)
    # Privacy removal is deliberate; retained segments keep their resource spelling.
    query = "&".join(
        segment
        for segment in p.query.split("&")
        if not SENSITIVE.search(unquote_plus(segment.partition("=")[0]))
        and not TRACKING.search(unquote_plus(segment.partition("=")[0]))
    )
    host = p.hostname.lower() if p.hostname else ""
    if ":" in host:
        host = "[" + host + "]"
    if p.port:
        host += ":" + str(p.port)
    return urlunsplit((p.scheme.lower(), host, path or "/", query, p.fragment if fragment else ""))


def inventory():
    rows = []
    category, section = "", ""
    for lineno, line in enumerate(MAP.read_text().splitlines(), 1):
        if line.startswith("## "):
            category = re.sub(r"（\d+ 个入口）", "", line[3:])
        elif line.startswith("### "):
            section = line[4:]
        match = re.search(r"https?://[^\s|]+", line)
        if not line.startswith("|") or not match:
            continue
        raw = match.group()
        url = clean_url(raw)
        rows.append(
            {
                "id": f"dd-{len(rows) + 1:03d}",
                "line": lineno,
                "category": category,
                "section": section,
                "name": line[1 : match.start()].strip(" |"),
                "url": url,
                "map_url": clean_url(raw, fragment=True),
                "url_normalized": url != raw,
                "fragment_route": urlsplit(raw).fragment,
                "map_noise": lineno >= 791,
                "recipe": {"family": "public-entry-probe.v1", "method": "GET", "url": url},
            }
        )
    return rows


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = inventory()
    write_json(
        args.output, {"map_sha256": hashlib.sha256(MAP.read_bytes()).hexdigest(), "rows": rows}
    )
    print(json.dumps({"rows": len(rows), "unique_requests": len({r["url"] for r in rows})}))
