"""Public Vietnamese bytes and live detail GET; independent lxml/date oracle."""

import json
from datetime import datetime
from pathlib import Path

from lxml import html

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")


def exercise_vietnam(h, live=False):
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    pages = {
        "/vietnam/detail/219914": "vietnam-48.html",
        "/vietnam/detail/219249": "vietnam-335.html",
        "/vietnam/year": "vietnam-year-2026.html",
        "/vietnam/category": "vietnam-category-6.html",
    }

    def get(request):
        if request.path not in pages:
            return original(request)
        body = (FIXTURES / pages[request.path]).read_bytes()
        h.site.ledger.append({"method": "GET", "path": request.path, "status": 200})
        request.send_response(200)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = get
    try:
        version = h.cli("recipe", "pack", ROOT / "recipes/vietnam_legal")["recipe_version"]
        h.config(
            "vietnam",
            entries=[h.site.url + "/vietnam/detail/219914"],
            identity="business_key",
            output_schema="record.v1",
            seed_role="detail",
            delay=0.0,
            budget={"requests": 10, "seconds": 25, "response_bytes": 500000},
        )
        binding = h.binding("vietnam", version, {"membership_urls": [h.site.url + "/vietnam/year"]})
        run = h.cli("collect", "vietnam", "--binding", binding)
        exported = h.cli("export", "vietnam", "--run", run["run_id"])
        h.capture("vietnam-48", {"receipt": run, "export": exported})
        check_detail(h, exported["records"][0], "Thủ tướng Chính phủ", None)
        h.check(
            "vietnam_source_keys_precise",
            set(exported["records"][0]["extracted_data"]),
            {"Lĩnh vực:", "Cơ quan ban hành:", "Năm ban hành:"},
        )
        h.check(
            "vietnam_no_field_state",
            set(exported["records"][0]["data"]),
            {"subject_areas", "issuing_authority", "issued_year"},
        )
        before = len(h.site.ledger)
        replay = h.cli("replay", run["run_id"])
        repeated = h.cli("export", "vietnam", "--run", replay["run_id"])
        h.check("vietnam_replay_zero_network", len(h.site.ledger), before)
        h.check(
            "vietnam_replay_data", repeated["records"][0]["data"], exported["records"][0]["data"]
        )
        h.check(
            "vietnam_replay_evidence",
            repeated["records"][0]["field_evidence"],
            exported["records"][0]["field_evidence"],
        )
        h.capture("vietnam-replay", {"receipt": replay, "export": repeated})

        h.config(
            "vietnam_category",
            entries=[h.site.url + "/vietnam/detail/219249"],
            identity="business_key",
            output_schema="record.v1",
            seed_role="detail",
            delay=0.0,
            budget={"requests": 10, "seconds": 25, "response_bytes": 500000},
        )
        binding = h.binding(
            "vietnam_category", version, {"membership_urls": [h.site.url + "/vietnam/category"]}
        )
        run = h.cli("collect", "vietnam_category", "--binding", binding)
        exported = h.cli("export", "vietnam_category", "--run", run["run_id"])
        h.capture("vietnam-335", {"receipt": run, "export": exported})
        check_detail(h, exported["records"][0], "Chính phủ", ["Chính sách xã hội"])
        if live:
            live_detail(h, version)
    finally:
        handler.do_GET = original


def object_bytes(h, key):
    import hashlib

    body = (h.root / "archive/objects" / key[:2] / key[2:]).read_bytes()
    h.check("vietnam_digest_" + key, hashlib.sha256(body).hexdigest(), key)
    return body


def check_detail(h, record, authority, subjects):
    h.check("vietnam_authority", record["data"]["issuing_authority"], authority)
    h.check("vietnam_year", record["data"]["issued_year"], 2026)
    h.check("vietnam_subjects", record["data"]["subject_areas"], subjects)
    for key, evidence in record["field_evidence"].items():
        snapshot = json.loads(object_bytes(h, evidence["snapshot_id"]))
        doc = html.fromstring(object_bytes(h, snapshot["body_hash"]).decode("utf-8"))
        node = doc.xpath(evidence["locator"]["path"])
        raw = " ".join(node[0].itertext()).strip()
        h.check("vietnam_raw_" + key, raw, evidence["raw_value"])
        expected = (
            datetime.strptime(raw, "%d-%m-%Y").year
            if key == "issued_year"
            else [raw]
            if key == "subject_areas"
            else raw
        )
        h.check("vietnam_independent_" + key, expected, record["data"][key])
    if subjects is None:
        h.check("vietnam_null_without_evidence", "subject_areas" in record["field_evidence"], False)
    year_proofs = record["field_evidence"]["issued_year"].get("supporting_locators", [])
    h.check("vietnam_year_membership_retained", len(year_proofs) > 0)
    for proof in year_proofs:
        snapshot = json.loads(object_bytes(h, proof["snapshot_id"]))
        doc = html.fromstring(object_bytes(h, snapshot["body_hash"]).decode("utf-8"))
        h.check("vietnam_year_membership_value", doc.xpath(proof["path"]), ["2026"])


def live_detail(h, version):
    # Runtime deliberately does not broaden its reviewed POST contract for ASP.NET state.
    h.config(
        "vietnam_live",
        entries=["https://vanban.chinhphu.vn/?pageid=27160&docid=219914"],
        allowed_hosts=["vanban.chinhphu.vn"],
        allowed_path_prefixes=["/"],
        scope="User-authorized official public legal detail, bounded one-document field acceptance",
        identity="business_key",
        output_schema="record.v1",
        seed_role="detail",
        delay=0.1,
        budget={"requests": 5, "seconds": 30, "response_bytes": 500000},
    )
    binding = h.binding("vietnam_live", version)
    run = h.cli("collect", "vietnam_live", "--binding", binding)
    exported = h.cli("export", "vietnam_live", "--run", run["run_id"])
    record = exported["records"][0]
    h.capture("vietnam-live", {"receipt": run, "export": exported})
    # No reviewed Runtime POST membership input was supplied, so this direct GET has
    # the independently reproducible date year but no category assignment or year-list proof.
    h.check("vietnam_live_authority", record["data"]["issuing_authority"], "Thủ tướng Chính phủ")
    h.check("vietnam_live_year", record["data"]["issued_year"], 2026)
    h.check("vietnam_live_subject_null", record["data"]["subject_areas"], None)
    before = len(h.site.ledger)
    replay = h.cli("replay", run["run_id"])
    repeated = h.cli("export", "vietnam_live", "--run", replay["run_id"])
    h.check("vietnam_live_replay_no_fixture_http", len(h.site.ledger), before)
    h.check("vietnam_live_replay_data", repeated["records"][0]["data"], record["data"])
    h.capture("vietnam-live-replay", {"receipt": replay, "export": repeated})
