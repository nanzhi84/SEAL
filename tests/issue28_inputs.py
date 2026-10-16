"""Source-input and parser-fingerprint behavior for independent Issue #28 E2E."""

import hashlib
import json

import yaml
from issue28_runtime import pdf_bytes


def adjust(path, **values):
    config = yaml.safe_load(path.read_text())
    for key, value in values.items():
        if key == "parser":
            config["crawl4ai"].update(value)
        else:
            config[key] = value
    path.write_text(yaml.safe_dump(config))


def latest(rt, source):
    rows = rt.versions(source)
    rt.check(source + " has successful version", bool(rows))
    return rows[-1]


def fingerprint_matrix(rt):
    fixture = rt.fixture
    fixture.html("/matrix", "SOURCE ALPHA")
    config = rt.config("dd-017", ["/matrix"])
    rt.collect(config)
    first = latest(rt, "dd-017")
    rt.check("first success is V1", first["sequence"] == 1)
    rt.collect(config)
    rt.check(
        "stable input creates new observation without new version",
        len(rt.versions("dd-017")) == 1 and len(rt.observations("dd-017")) == 2,
    )
    first_output = rt.archive(first)["result"]["markdown"]
    adjust(
        config,
        concurrency=2,
        recheck_seconds=17,
        budget={"requests": 80, "seconds": 65, "response_bytes": 2500000},
    )
    rt.collect(config)
    rt.check(
        "scheduling budget and concurrency do not create version", len(rt.versions("dd-017")) == 1
    )
    fixture.html("/matrix", "SOURCE BRAVO")
    rt.collect(config)
    second = latest(rt, "dd-017")
    rt.check("only source change creates exactly V2", len(rt.versions("dd-017")) == 2)
    before_output = rt.archive(second)["result"]["markdown"]
    adjust(config, parser={"word_count_threshold": 2})
    rt.check(
        "deploy/config change without observation creates no version",
        len(rt.versions("dd-017")) == 2,
    )
    rt.collect(config)
    third = latest(rt, "dd-017")
    rt.check("semantic parser configuration change creates V3", len(rt.versions("dd-017")) == 3)
    rt.check(
        "parser change creates version even with identical output",
        rt.archive(third)["result"]["markdown"] == before_output,
    )
    rt.check(
        "fingerprints explain source-only and parser-only changes",
        first["body_hash"] != second["body_hash"]
        and first["parser_fingerprint"] == second["parser_fingerprint"]
        and second["body_hash"] == third["body_hash"]
        and second["parser_fingerprint"] != third["parser_fingerprint"],
    )
    fixture.html("/matrix", "SOURCE CHARLIE")
    adjust(config, parser={"word_count_threshold": 3})
    rt.collect(config)
    fourth = latest(rt, "dd-017")
    rt.check(
        "both changes create V4 and distinct fingerprints",
        len(rt.versions("dd-017")) == 4
        and third["body_hash"] != fourth["body_hash"]
        and third["parser_fingerprint"] != fourth["parser_fingerprint"],
    )
    rt.collect(config)
    rt.check(
        "stable new parser does not repeatedly create versions", len(rt.versions("dd-017")) == 4
    )
    fixture.html("/matrix", "temporary error", status=503)
    rt.collect(config, success=False)
    current = rt.query(
        "SELECT current_version_id FROM seal.resource WHERE id=%s", (first["resource_id"],)
    )[0]
    rt.check(
        "failed fetch retains current success and version count",
        current["current_version_id"] == fourth["id"] and len(rt.versions("dd-017")) == 4,
    )
    failed = rt.observations("dd-017")[-1]
    rt.check(
        "failed fetch retains observation without success pointer", failed["version_id"] is None
    )
    rt.check("failed observation explains stage or diagnostic", bool(failed["error"]))
    fixture.html("/matrix", "SOURCE CHARLIE")
    rt.collect(config)
    rt.check("recovery of unchanged successful input reuses V4", len(rt.versions("dd-017")) == 4)
    rt.save(
        "matrix.json",
        {
            "versions": rt.versions("dd-017"),
            "observations": rt.observations("dd-017"),
            "first_markdown": first_output,
        },
    )
    return first, fourth


def input_modes(rt):
    fixture = rt.fixture
    fixture.html("/history", "HISTORY ALPHA")
    config = rt.config("dd-040", ["/history", "/second", "/never"])
    fixture.html("/second", "SECOND STABLE")
    fixture.html("/never", "ALWAYS FAIL", status=403)
    for text in ("HISTORY ALPHA", "HISTORY BRAVO", "HISTORY ALPHA"):
        fixture.html("/history", text)
        rt.collect(config, success=False)
    history = [x for x in rt.versions("dd-040") if x["url"].endswith("/history")]
    rt.check(
        "A to B to A retains three historical versions",
        len(history) == 3
        and history[0]["version_fingerprint"] == history[2]["version_fingerprint"],
    )
    rt.check(
        "never successful resource has no version",
        not [x for x in rt.versions("dd-040") if x["url"].endswith("/never")],
    )
    adjust(config, parser={"mode": "rendered_dom"})
    code, result = rt.collect(config, success=False)
    rt.check(
        "input mode change requires new source",
        code != 0 and "input_mode_change_requires_new_source" in json.dumps(result),
    )

    fixture.pages["/plain.txt"] = (
        200,
        "text/plain",
        b"PLAIN ALPHA public content with many distinct meaningful words for text acceptance. "
        * 20,
    )
    txt = rt.config("dd-041", ["/plain.txt"])
    rt.collect(txt)
    rt.collect(txt)
    rt.check("plain text stable body reuses one version", len(rt.versions("dd-041")) == 1)
    fixture.pages["/plain.txt"] = (
        200,
        "text/plain",
        b"PLAIN BRAVO public content with many distinct meaningful words for text acceptance. "
        * 20,
    )
    rt.collect(txt)
    rt.check("plain text source change versions", len(rt.versions("dd-041")) == 2)

    fixture.pages["/document.pdf"] = (
        200,
        "application/pdf",
        pdf_bytes("PDF ALPHA public text layer acceptance"),
    )
    pdf = rt.config("dd-448", ["/document.pdf"])
    rt.collect(pdf)
    old_pdf = latest(rt, "dd-448")
    rt.collect(pdf)
    rt.check("stable PDF input reuses one version", len(rt.versions("dd-448")) == 1)
    rt.check(
        "PDF raw attachment participates in source digest",
        old_pdf["body_hash"] == hashlib.sha256(fixture.pages["/document.pdf"][2]).hexdigest(),
    )
    fixture.pages["/document.pdf"] = (
        200,
        "application/pdf",
        pdf_bytes("PDF BRAVO public text layer acceptance"),
    )
    rt.collect(pdf)
    new_pdf = latest(rt, "dd-448")
    rt.check(
        "PDF bytes change creates distinct source and result",
        len(rt.versions("dd-448")) == 2 and old_pdf["body_hash"] != new_pdf["body_hash"],
    )
    fixture.pages["/document.pdf"] = (200, "application/pdf", b"not a valid PDF")
    rt.collect(pdf, success=False)
    rt.check(
        "parse failure leaves successful PDF pointer",
        rt.query(
            "SELECT current_version_id FROM seal.resource WHERE id=%s", (new_pdf["resource_id"],)
        )[0]["current_version_id"]
        == new_pdf["id"],
    )

    fixture.html(
        "/dom",
        "DOM INPUT",
        script='<script>document.querySelector("h1").textContent="DOM ALPHA";</script>',
    )
    dom = rt.config(
        "dd-018",
        ["/dom"],
        crawl4ai={
            "mode": "rendered_dom",
            "max_depth": 0,
            "page_timeout_ms": 15000,
            "word_count_threshold": 1,
            "wait_until": "load",
        },
    )
    rt.collect(dom)
    rt.collect(dom)
    rt.check("stable rendered DOM reuses version", len(rt.versions("dd-018")) == 1)
    fixture.html(
        "/dom",
        "DOM INPUT",
        script='<script>document.querySelector("h1").textContent="DOM BRAVO";</script>',
    )
    rt.collect(dom)
    rt.check(
        "rendered DOM change versions parsed rendered content",
        len(rt.versions("dd-018")) == 2
        and "DOM BRAVO" in json.dumps(rt.archive(latest(rt, "dd-018"))),
    )
    rt.save(
        "input-contracts.json",
        {
            source: {"versions": rt.versions(source), "observations": rt.observations(source)}
            for source in ("dd-040", "dd-041", "dd-448", "dd-018")
        },
    )
    return history, old_pdf, new_pdf
