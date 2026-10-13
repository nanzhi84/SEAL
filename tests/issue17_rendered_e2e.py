"""专用 headless 浏览器的真实 DOM、子请求和 redirect 范围验收。"""

from __future__ import annotations

import hashlib
import json

import yaml
from issue17_support import json_write


def page(fixture, label):
    return (
        '<!doctype html><html><head><title>Rendered acceptance</title></head><body>'
        '<main><h1>Public dynamic document</h1><div id="target">Initial</div>'
        '<a href="/rendered/redirect">Redirect trap</a>'
        '<img src="/outside?browser-asset" alt="scope trap"></main>'
        '<script>document.getElementById("target").textContent=' + json.dumps(label) + ';'
        'fetch("/outside").catch(()=>{});'
        'fetch("http://localhost:' + str(fixture.server.server_port) + '/outside?foreign-host").catch(()=>{});'
        '</script></body></html>'
    ).encode()


def run_rendered(acceptance):
    lab, fixture = acceptance.lab, acceptance.fixture
    primary_config = acceptance.config.read_text()
    before = acceptance.versions()
    switched = yaml.safe_load(primary_config)
    switched["crawl4ai"]["mode"] = "rendered_dom"
    acceptance.config.write_text(yaml.safe_dump(switched, sort_keys=False))
    try:
        _, receipt = acceptance.collect(expected=(1, 2))
        lab.check("existing HTTP timeline rejects rendered mode comparison", "input_mode_change_requires_new_source" in json.dumps(receipt))
        lab.check("input mode switch preserves historical current and Versions", acceptance.versions() == before)
    finally:
        acceptance.config.write_text(primary_config)
    config = yaml.safe_load(primary_config)
    config.update({"id": "dd-003", "entry_urls": [fixture.base + "/rendered/"],
                   "allowed_path_prefixes": ["/rendered/"], "concurrency": 1})
    config["crawl4ai"].update({"mode": "rendered_dom", "max_depth": 1})
    path = lab.work / "dd-003.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    acceptance.configs["dd-003"] = path
    fixture.set("/rendered/redirect", status=302, mime="text/html", body=b"redirect trap")
    fixture.headers["/rendered/redirect"] = {"Location": "/outside"}
    original = page(fixture, "Rendered Alpha")
    fixture.set("/rendered/", body=original)
    outside_before = sum(row["path"].startswith("/outside") for row in fixture.requests)
    acceptance.collect("dd-003", expected=(0, 1))
    versions = acceptance.count(1, path="/rendered/", source_id="dd-003", label="JavaScript rendered initial Version")
    observations = acceptance.observations("/rendered/", "dd-003")
    observed = next(row for row in reversed(observations) if row["version_id"] is not None)
    lab.check("DOM observation explicitly marks rendered_dom", observed["input_mode"] == "rendered_dom")
    body_hash = observed["body_hash"]
    archive = lab.archive / "objects" / body_hash[:2] / body_hash[2:]
    rendered = archive.read_bytes()
    lab.check("rendered input archive reflects actual JavaScript DOM change", b'<div id="target">Rendered Alpha</div>' in rendered)
    lab.check("DOM hash uses rendered bytes rather than raw HTTP body", hashlib.sha256(rendered).hexdigest() == body_hash and body_hash != hashlib.sha256(original).hexdigest())
    result = versions[0]["crawl_result"]["result"]
    lab.check("CrawlResult uses same rendered DOM as archived input", result.get("html", "").encode() == rendered)
    outside_after = sum(row["path"].startswith("/outside") for row in fixture.requests)
    lab.check("browser out-of-scope subrequests and foreign host blocked at network", outside_after == outside_before)
    redirect_versions = acceptance.versions("/rendered/redirect", "dd-003")
    lab.check("out-of-scope redirect does not create content Version", not redirect_versions)
    acceptance.collect("dd-003", expected=(0, 1))
    acceptance.count(1, path="/rendered/", source_id="dd-003", label="unchanged rendered DOM does not add Version")
    fixture.set("/rendered/", body=page(fixture, "Rendered Beta"))
    acceptance.collect("dd-003", expected=(0, 1))
    acceptance.count(2, path="/rendered/", source_id="dd-003", label="changed rendered DOM appends Version")
    json_write(lab.artifacts / "rendered-dom.database.json", lab.snapshot("rendered-dom"))
    (lab.artifacts / "dd-003.yaml").write_text(path.read_text())
