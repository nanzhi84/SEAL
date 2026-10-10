"""Real CLI/PG/HTTP regression for resource-sensitive URL spelling.

Failure modes: reserved escapes decoded/encoded, query reordered or rebuilt,
resources merged by scheduler fingerprints, archive identity changed, replay HTTP.
"""

import argparse
import html
import sys
import traceback
from pathlib import Path

from acceptance import Harness

PATHS = [
    "/url/detail!action?q=!",
    "/url/detail%21action?q=%21",
    "/url/detail?q=a+b",
    "/url/detail?q=a%20b",
    "/url/detail?a=1&a=2",
    "/url/detail?a=2&a=1",
    "/url/detail?q=%2f",
    "/url/detail?q=%2F",
    "/url/detail?bare&empty=&x=1&&",
]


def exercise(h):
    failures = []

    def check(name, actual, expected):
        try:
            h.check(name, actual, expected)
        except AssertionError:
            failures.append(name)

    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET

    def get(request):
        if not request.path.startswith("/url/"):
            return original(request)
        h.site.ledger.append({"method": "GET", "path": request.path, "status": 200})
        if request.path == "/url/list":
            content = (
                '<div class="documents">'
                + "".join(
                    '<a href="' + html.escape(path, quote=True) + '">Document</a>'
                    for path in PATHS + PATHS[:1]
                )
                + "</div>"
            )
        else:
            content = (
                "<h1>"
                + html.escape(request.path)
                + "</h1><article>Resource-specific public content.</article>"
            )
        body = content.encode()
        request.send_response(200)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = get
    try:
        h.config("url_semantics", entries=[h.site.url + "/url/list"], seed_role="list", delay=0.0)
        binding = h.binding("url_semantics")
        receipt = h.cli("run", "url_semantics", "--binding", binding)
        export = h.cli("export", "url_semantics", "--run", receipt["run_id"])
        h.capture(
            "url-observations", {"expected_paths": PATHS, "receipt": receipt, "export": export}
        )
        check("url_collect_status", receipt["status"], "complete")
        check(
            "url_wire_spelling_and_exact_duplicate_dedup",
            sorted(x["path"] for x in h.site.ledger),
            sorted(["/url/list"] + PATHS),
        )
        docs = export["documents"]
        check("url_distinct_resource_count", len(docs), len(PATHS))
        check(
            "url_document_identity_preserves_spelling",
            sorted(d["url"] for d in docs),
            sorted(h.site.url + p for p in PATHS),
        )
        offset = len(h.site.ledger)
        replay = h.cli("replay", receipt["run_id"])
        check("url_replay_status", replay["status"], "complete")
        check("url_replay_zero_http", len(h.site.ledger), offset)
        offset = len(h.site.ledger)
        h.config(
            "url_frozen", entries=[h.site.url + p for p in PATHS], seed_role="detail", delay=0.0
        )
        frozen_binding = h.binding("url_frozen")
        frozen = h.cli("run", "url_frozen", "--binding", frozen_binding)
        check("url_frozen_entries_status", frozen["status"], "complete")
        check(
            "url_frozen_entries_wire",
            sorted(x["path"] for x in h.site.ledger[offset:]),
            sorted(PATHS),
        )
        frozen_export = h.cli("export", "url_frozen", "--run", frozen["run_id"])
        check("url_frozen_document_count", len(frozen_export["documents"]), len(PATHS))
        offset = len(h.site.ledger)
        recheck = h.cli("run", "url_frozen", "--binding", frozen_binding, "--recheck")
        check("url_recheck_status", recheck["status"], "complete")
        check("url_recheck_wire", sorted(x["path"] for x in h.site.ledger[offset:]), sorted(PATHS))
        if failures:
            raise AssertionError(",".join(failures))
    finally:
        handler.do_GET = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    h = Harness(args.output)
    error = None
    try:
        h.start()
        exercise(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("url-semantics", error)
        h.close()
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
