"""Independent Issue 31 business scenarios; product access uses CLI, SQL, S3 and HTTP."""
# ruff: noqa: F403,F405
from issue31_common import *

# ============================================================ versioning (dd-050)


def version_page(name, variant):
    return structure_detail(f"Versioned {name}", extra=f"<p>Version body {name} variant {variant}. Stable sentence for the document.</p>")


def setup_versioning(lab):
    fx = Fixture()
    S["fx_ver"] = fx
    fx.put("/v/detail/x", version_page("x", "A"))
    fx.put("/v/detail/y", version_page("y", "A"))
    fx.put("/v/list", html_page("Version list", "<p>Updated 2026-10-01</p>", links=[("/v/detail/x", "X"), ("/v/detail/y", "Y")]))
    S["ver_cfg"] = base_source("dd-050", fx, ["/v/list"], documents={"detail_url_patterns": [r".*/v/detail/.*"]})
    return fx


def ver_state(lab, path):
    url = S["fx_ver"].base + path
    res = lab.q("SELECT id, current_version_id FROM seal.resource WHERE external_key=%s", (url,))
    if not res:
        return {"resource": None, "versions": [], "observations": []}
    rid = res[0]["id"]
    versions = lab.q("SELECT id, sequence, change_reason, input_fingerprint, parser_fingerprint, content_hash FROM seal.version WHERE resource_id=%s ORDER BY sequence", (rid,))
    obs = lab.q("SELECT outcome, change_reason, error, version_id, observed_at FROM seal.observation WHERE resource_id=%s ORDER BY observed_at", (rid,))
    return {"resource": rid, "current": res[0]["current_version_id"], "versions": versions, "observations": obs}


def test_version_first_collect(lab):
    """Versioning source first collect: first_success versions."""
    setup_versioning(lab)
    result, receipt, _ = collect(lab, "dd-050", S["ver_cfg"], label="ver r1")
    lab.check("r1 exit 0", result.returncode == 0, result.stderr[-1000:])
    for p in ("/v/detail/x", "/v/detail/y"):
        st = ver_state(lab, p)
        lab.check(f"{p} one first_success version", len(st["versions"]) == 1 and st["versions"][0]["change_reason"] == "first_success", st)


# ============================================================ versioning continued


def test_version_listing_reorder_unchanged(lab):
    """Re-ordering the listing (and listing-only text) keeps document versions."""
    fx = S["fx_ver"]
    fx.put("/v/list", html_page("Version list", "<p>Updated 2026-10-15</p>", links=[("/v/detail/y", "Y"), ("/v/detail/x", "X")]))
    result, receipt, _ = collect(lab, "dd-050", S["ver_cfg"], label="ver r2 reorder")
    lab.check("r2 exit 0", result.returncode == 0, result.stderr[-1000:])
    for p in ("/v/detail/x", "/v/detail/y"):
        st = ver_state(lab, p)
        lab.check(f"{p} still one version", len(st["versions"]) == 1, st)
        lab.check(f"{p} latest observation unchanged", st["observations"][-1]["outcome"] == "unchanged", st["observations"])


def test_version_real_change_and_unpublished_hash(lab):
    """Real input change creates source_changed version; unpublished hash is 404 until publish."""
    fx, api = S["fx_ver"], S["api"]
    fx.put("/v/detail/x", version_page("x", "B"))
    result, receipt, _ = collect(lab, "dd-050", S["ver_cfg"], label="ver r3 change")
    lab.check("r3 exit 0", result.returncode == 0, result.stderr[-1000:])
    st = ver_state(lab, "/v/detail/x")
    lab.check("x has 2 versions", len(st["versions"]) == 2, st)
    lab.check("x v2 change_reason source_changed", st["versions"][-1]["change_reason"] == "source_changed", st["versions"])
    lab.check("y unchanged", len(ver_state(lab, "/v/detail/y")["versions"]) == 1, ver_state(lab, "/v/detail/y"))
    new = rows_for(lab, "dd-050", fx.base + "/v/detail/x")[0]
    result, receipt = lab.cli_json("snapshot", "create", label="snapshot create #2 (unpublished)")
    lab.check("snapshot #2 created", result.returncode == 0, result.stderr[-1000:])
    S["snap2"] = receipt.get("snapshot_id")
    r = api.get("/files/" + new["markdown_hash"], "all")
    lab.check("hash of sealed-but-unpublished version is 404", r.status_code == 404, r.status_code)
    r = api.get("/files/" + new["file_hash"], "all")
    lab.check("original of unpublished version is 404", r.status_code == 404, r.status_code)
    items, _, snap = api.all_results("all", source_id="dd-050")
    lab.check("/results still serves published snapshot #1", str(snap) == str(S["snap1"]) and new["markdown_hash"] not in {i["markdown_hash"] for i in items}, snap)
    result, _ = lab.cli_json("snapshot", "publish", S["snap2"], label="snapshot publish #2")
    lab.check("publish #2 exit 0", result.returncode == 0, result.stderr[-1000:])
    r = api.get("/files/" + new["markdown_hash"], "all")
    lab.check("after publish the new markdown is served byte-exact", r.status_code == 200 and sha256(r.content) == new["markdown_hash"], r.status_code)
    old = next(i for i in S["api_items"] if i["来源详情页"] == fx.base + "/v/detail/x")
    r = api.get("/files/" + old["markdown_hash"], "all")
    lab.check("superseded (no longer published) markdown is 404", r.status_code == 404, r.status_code)


def test_version_a_b_a_three_versions(lab):
    """A→B→A keeps three immutable versions."""
    fx = S["fx_ver"]
    fx.put("/v/detail/x", version_page("x", "A"))
    result, receipt, _ = collect(lab, "dd-050", S["ver_cfg"], label="ver r4 back to A")
    lab.check("r4 exit 0", result.returncode == 0, result.stderr[-1000:])
    st = ver_state(lab, "/v/detail/x")
    lab.save("evidence/version-x-history.json", st)
    lab.check("x has 3 versions", len(st["versions"]) == 3, st["versions"])
    v1, v2, v3 = st["versions"]
    lab.check("v3 is a new version id (not reuse of v1)", v3["id"] != v1["id"] and str(st["current"]) == str(v3["id"]), st)
    lab.check("v1 and v3 share content/input fingerprints, v2 differs", v1["content_hash"] == v3["content_hash"] != v2["content_hash"]
              and v1["input_fingerprint"] == v3["input_fingerprint"], [v["content_hash"] for v in st["versions"]])


def test_version_parser_change(lab):
    """Parser/config change (quality accept routes) creates parser_changed versions."""
    cfg = json.loads(json.dumps(S["ver_cfg"]))
    cfg["parser"]["quality_accept_routes"] = ["accept", "accept_ocr"]
    result, receipt, _ = collect(lab, "dd-050", cfg, label="ver r5 parser change")
    lab.check("r5 exit 0", result.returncode == 0, result.stderr[-1000:])
    for p in ("/v/detail/x", "/v/detail/y"):
        st = ver_state(lab, p)
        last = st["versions"][-1]
        prev = st["versions"][-2] if len(st["versions"]) > 1 else None
        lab.check(f"{p} new version with parser_changed", prev is not None and last["change_reason"] == "parser_changed", st["versions"])
        lab.check(f"{p} input fingerprint unchanged, parser fingerprint changed",
                  prev is not None and prev["input_fingerprint"] == last["input_fingerprint"] and prev["parser_fingerprint"] != last["parser_fingerprint"],
                  st["versions"][-2:])


def test_nondeterministic_output_conflict(lab):
    """Same input + same parser (br-ocr) but different output: explicit conflict, no new version."""
    mock, fx = S["br"], S["fx_br"]
    mock.result_factory = lambda req, raw: br_mixed_result("BRMOCK-9999")
    url = fx.base + "/br/mixed.pdf"
    before = ver_state_any(lab, url)
    result, receipt, _ = collect(lab, "dd-041", S["br_cfg"], label="br-ocr nondeterministic", env_extra={"SEAL_BR_OCR_URL": mock.base})
    lab.save("receipts/dd-041-nondeterministic.json", receipt or result.stdout)
    after = ver_state_any(lab, url)
    lab.save("evidence/nondeterministic.json", {"before": before, "after": after})
    lab.check("no new version created", len(after["versions"]) == len(before["versions"]) == 1, after["versions"])
    lab.check("current version unchanged", after["current"] == before["current"], [before["current"], after["current"]])
    last = after["observations"][-1]
    lab.check("failed observation nondeterministic_parser_output", last["outcome"] == "failed" and "nondeterministic" in json.dumps(last, default=str), last)
    conflicts = lab.q("SELECT reason FROM seal.delivery_conflict WHERE resource_id=%s", (after["resource"],))
    lab.check("delivery_conflict recorded", any("nondeterministic" in (c["reason"] or "") for c in conflicts), conflicts)


def ver_state_any(lab, url):
    res = lab.q("SELECT id, current_version_id FROM seal.resource WHERE external_key=%s", (url,))
    rid = res[0]["id"]
    return {"resource": rid, "current": res[0]["current_version_id"],
            "versions": lab.q("SELECT id, sequence, change_reason, content_hash FROM seal.version WHERE resource_id=%s ORDER BY sequence", (rid,)),
            "observations": lab.q("SELECT outcome, change_reason, error, version_id FROM seal.observation WHERE resource_id=%s ORDER BY observed_at", (rid,))}


