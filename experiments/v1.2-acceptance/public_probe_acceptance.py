"""Metadata-only probe contracts through the existing loopback Site and CLI."""

import hashlib
import json
import subprocess
import sys
from urllib.parse import urlsplit

from probe import Probe

FORBIDDEN_BODY_KEYS = {
    "title",
    "excerpt",
    "text_chars",
    "links",
    "inputs",
    "scripts",
    "iframes",
    "forms",
    "candidates",
    "pagination",
    "article_blocks",
    "api_hints",
    "raw_path",
}


def run_public_probe(h):
    # Failure modes precede the implementation: readable sensitive values in
    # extracted metadata; archived bodies despite opt-out; login/challenge
    # redirect targets or a second candidate requested after a clear refusal.
    failures = []

    def check(name, actual, expected=True):
        try:
            h.check(name, actual, expected)
        except AssertionError:
            failures.append(name)
        h.assertions[-1]["evidence"] = "public-probe-observations.json; request-ledger.json"

    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    html = (
        b"<html><h1>synthetic-private-value</h1><article>"
        b'password="synthetic-private-value"; Public notice content repeated '
        b"for this metadata-only boundary fixture.</article></html>"
    )
    routes = {
        "/probe/sensitive-html": (200, None, html),
        "/probe/login-redirect": (302, "/cas/login?poc_token=synthetic-private-value", b""),
        "/probe/captcha-redirect": (
            302,
            "/mp/wappoc_appmsgcaptcha?poc_token=synthetic-private-value",
            b"",
        ),
        "/probe/denied": (403, None, b"Access denied"),
        "/probe/second-candidate": (200, None, b"Second candidate must not be requested"),
        "/cas/login": (200, None, b"Login challenge must not be requested"),
        "/mp/wappoc_appmsgcaptcha": (200, None, b"Captcha must not be requested"),
    }

    def fixture_get(request):
        path = urlsplit(request.path).path
        if path not in routes:
            return original(request)
        status, location, body = routes[path]
        h.site.ledger.append({"method": "GET", "path": path, "status": status})
        request.send_response(status)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Content-Length", str(len(body)))
        if location:
            request.send_header("Location", location)
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = fixture_get
    summaries = []
    try:
        for label, path in (
            ("sensitive-header-body", "/sensitive/authorization"),
            ("sensitive-html-body", "/probe/sensitive-html"),
            ("safe-public-placeholders", "/sensitive/baseline"),
        ):
            output = h.root / label
            output.mkdir()
            (output / "bodies").mkdir()
            result = Probe(output).request(h.site.url + path, "synthetic", retain_body=False)
            encoded = json.dumps(result)
            forbidden = sorted(FORBIDDEN_BODY_KEYS.intersection(result))
            check(label + "_metadata_has_no_body_fields", forbidden, [])
            check(
                label + "_metadata_has_no_sensitive_values",
                "synthetic-private-value" not in encoded,
            )
            check(label + "_body_not_archived", list((output / "bodies").iterdir()), [])
            check(label + "_response_digest_present", len(result.get("sha256", "")), 64)
            check(label + "_http_observed", result.get("status"), 200)
            summaries.append(
                {
                    "fixture": label,
                    "status": result.get("status"),
                    "sha256": result.get("sha256"),
                    "forbidden_field_names": forbidden,
                    "sensitive_value_exposed": "synthetic-private-value" in encoded,
                    "raw_body_files": len(list((output / "bodies").iterdir())),
                }
            )
        for label, path, no_redirect, status in (
            ("login-refusal", "/probe/login-redirect", True, 302),
            ("captcha-refusal", "/probe/captcha-redirect", False, 302),
            ("http-refusal", "/probe/denied", False, 403),
        ):
            offset = len(h.site.ledger)
            output = h.root / label
            urls = h.root / (label + "-urls.json")
            # dd-016 only selects the existing CLI inventory slot. Both URLs
            # are isolated synthetic fixtures, never claims about that Source.
            urls.write_text(
                json.dumps({"dd-016": [h.site.url + path, h.site.url + "/probe/second-candidate"]})
            )
            command = [
                sys.executable,
                "experiments/v1.2-acceptance/expanded_probe.py",
                "--source",
                "dd-016",
                "--urls-file",
                str(urls),
                "--no-retain-body",
                "--output",
                str(output),
            ]
            if no_redirect:
                command.append("--no-redirect")
            process = subprocess.run(command, env=h.env, capture_output=True, text=True, timeout=30)
            check(label + "_cli_completed", process.returncode, 0)
            observations = json.loads((output / "results.json").read_text())[0]["observations"]
            ledger = h.site.ledger[offset:]
            check(label + "_one_observation_then_stop", len(observations), 1)
            check(label + "_only_initial_public_request", [r["path"] for r in ledger], [path])
            check(label + "_original_http_status_retained", observations[0].get("status"), status)
            check(label + "_body_not_archived", list((output / "bodies").iterdir()), [])
            evidence_bytes = b"".join(p.read_bytes() for p in output.rglob("*") if p.is_file())
            check(
                label + "_metadata_has_no_sensitive_values",
                b"synthetic-private-value" not in evidence_bytes,
            )
            summaries.append(
                {
                    "fixture": label,
                    "status": observations[0].get("status"),
                    "observation_count": len(observations),
                    "requests": ledger,
                    "cli_exit": process.returncode,
                    "metadata_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                }
            )
    finally:
        handler.do_GET = original
        h.capture("public-probe-observations", summaries)
    if failures:
        raise AssertionError("public_probe_contracts: " + ", ".join(failures))
