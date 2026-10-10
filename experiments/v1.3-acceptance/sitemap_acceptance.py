"""Real CLI/HTTP/PostgreSQL contracts for gzip sitemap archive and Replay."""

import argparse
import gzip
import hashlib
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/v1.2-acceptance"))
from acceptance import Harness  # noqa: E402


def exercise(h):
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    xml = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'
        + h.site.url
        + "/document!public</loc></url></urlset>"
    ).encode()
    compressed = gzip.compress(xml, mtime=0)
    corrupt = bytearray(compressed)
    corrupt[-8] ^= 128
    large = gzip.compress(xml + b" " * 200000, mtime=0)
    bodies = {
        "/plain.xml": xml,
        "/file.xml.gz": compressed,
        "/encoding.xml.gz": compressed,
        "/corrupt.xml.gz": bytes(corrupt),
        "/oversize.xml.gz": large,
    }

    def get(request):
        path = request.path
        content_type, encoding = "application/xml", None
        if path == "/robots.txt":
            body, content_type = b"User-agent: *\nAllow: /\n", "text/plain"
        elif path in bodies:
            body = bodies[path]
            if path != "/plain.xml":
                content_type = "application/gzip"
            if path == "/encoding.xml.gz":
                encoding = "gzip"
        elif path == "/document!public":
            body = (
                "<html><h1>Public legal notice</h1><article><h2>第一条 公开规定</h2>"
                "<p>这个公开 HTTP 样本用于验证压缩 Sitemap 的真实调度、归档和零网络重放。"
                "正文需要保留原始证据及条文次序，并且不会声称外部网站适配完成。</p>"
                "</article></html>"
            ).encode()
            content_type = "text/html; charset=utf-8"
        else:
            return original(request)
        h.site.ledger.append({"method": "GET", "path": path, "status": 200})
        request.send_response(200)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        if encoding:
            request.send_header("Content-Encoding", encoding)
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = get
    try:
        version = h.cli("recipe", "pack", "recipes/seeded")["recipe_version"]
        for label, path, error in (
            ("plain", "/plain.xml", None),
            ("gzip_file", "/file.xml.gz", None),
            ("http_encoding", "/encoding.xml.gz", None),
            ("corrupt", "/corrupt.xml.gz", "sitemap_decompression_failed"),
            ("oversize", "/oversize.xml.gz", "sitemap_size_exceeded"),
        ):
            source = "sitemap_" + label
            budget = {"requests": 10, "seconds": 25, "response_bytes": 1024 if error else 100000}
            h.config(
                source,
                entries=[h.site.url + path],
                output_schema="record.v1",
                robots=True,
                delay=0.0,
                budget=budget,
            )
            binding = h.binding(
                source, version, {"expand_homepage": False, "discover_sitemaps": False}
            )
            before = len(h.site.ledger)
            receipt = h.cli("collect", source, "--binding", binding, ok=error is None)
            inspection = h.cli("inspect", "run", receipt["run_id"])
            exported = h.cli("export", source, "--run", receipt["run_id"])
            fetched = [row["path"] for row in h.site.ledger[before:]]
            h.check(
                label + "_collect_status", receipt["status"], "partial" if error else "complete"
            )
            h.check(
                label + "_document_requests", fetched.count("/document!public"), 0 if error else 1
            )
            h.check(label + "_record_count", len(exported["records"]), 0 if error else 1)
            if error:
                h.check(label + "_explicit_diagnostic", error in receipt["errors"])
            archived_input = next(
                entry
                for entry in inspection["run"]["inputs"]
                if entry["logical_url"] == h.site.url + path
            )
            snapshot = h.cli("inspect", "snapshot", archived_input["snapshot_id"])
            expected_body = xml if label == "http_encoding" else bodies[path]
            h.check(
                label + "_archive_representation_hash",
                snapshot["snapshot"]["body_hash"],
                hashlib.sha256(expected_body).hexdigest(),
            )
            h.check(
                label + "_archive_representation_size",
                snapshot["snapshot"]["body_size"],
                len(expected_body),
            )
            h.check(
                label + "_archive_body_verified", snapshot["archive"]["availability"], "available"
            )
            before_replay = len(h.site.ledger)
            replay = h.cli("replay", receipt["run_id"], ok=error is None)
            replay_export = h.cli("export", source, "--run", replay["run_id"])
            h.check(label + "_replay_zero_http", len(h.site.ledger), before_replay)
            h.check(label + "_replay_status", replay["status"], receipt["status"])
            h.check(
                label + "_replay_structured_output",
                [record["data"] for record in replay_export["records"]],
                [record["data"] for record in exported["records"]],
            )
            if error:
                h.check(label + "_replay_same_diagnostic", error in replay["errors"])
            h.capture(label, {"collect": receipt, "replay": replay, "snapshot": snapshot})
    finally:
        handler.do_GET = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    h, error = Harness(args.output), None
    h.env["SEAL_USE_ENV_PROXY"] = "0"
    try:
        h.start()
        exercise(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("v1.3-sitemap-compression", error)
        manifest_path = args.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(
            version="v1.3",
            command="uv run --frozen python experiments/v1.3-acceptance/sitemap_acceptance.py --output <new-directory>",
        )
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        h.close()
    print(args.output.resolve())
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
