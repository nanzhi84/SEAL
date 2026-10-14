"""Issue23 lifecycle, historical token, and live permission acceptance."""
from __future__ import annotations

import copy
import json
from collections import Counter
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.fernet import Fernet
from issue20_fixture import html
from issue22_contract import next_url, old_token, tamper
from issue22_data import expected_content
from issue23_lab import digest, write
from psycopg.types.json import Jsonb


def path(entry, **parameters):
    return "/results?" + urlencode(dict(source_url=entry, **parameters))


def cursor_from(url):
    return parse_qs(urlsplit(url).query)["cursor"][0]


def stable(values):
    return [{**i, "attachments": [dict(name=a["name"], media_type=a["media_type"]) for a in i["attachments"]]}
            for i in values]


def check_public(lab, data, ident, source, entry=None):
    mapping = lab.mapping(ident)
    rows = [r for r in mapping["items"] if r["source_id"] == source]
    entry = entry or next(s["entry_urls"][0] for s in mapping["sources"] if s["source_id"] == source)
    status, _, values, _, _ = lab.request(path(entry, limit=100))
    lab.check("published HTTP resource set equals frozen SQL " + source,
              status == 200 and isinstance(values, list) and [i["url"] for i in values] == [r["url"] for r in rows],
              snapshot_id=ident, status_code=status, expected=len(rows), actual=len(values) if isinstance(values, list) else None)
    if not isinstance(values, list) or len(values) != len(rows):
        return values
    evidence = []
    for row, item in zip(rows, values, strict=True):
        envelope = json.loads(lab.body(row["result_ref"]))
        actual = {k: v for k, v in item.items() if k != "attachments"}
        lab.check("HTTP whitelist content exactly matches frozen archive", actual == expected_content(row, envelope),
                  resource_id=row["resource_id"], version_id=row["version_id"], result_ref=row["result_ref"])
        lab.check("attachment list equals frozen result refs", len(item["attachments"]) == len(envelope["attachment_refs"]))
        for a, ref in zip(item["attachments"], envelope["attachment_refs"], strict=True):
            download_status, _, _, raw, _ = lab.request(a["url"])
            lab.check("download original bytes match SQL/S3 hash", download_status == 200 and raw == lab.body(ref) and digest(raw) == ref,
                      sha256=digest(raw), expected_sha256=ref, bytes=len(raw))
            evidence.append(dict(resource_id=row["resource_id"], version_id=row["version_id"],
                                 attachment_sha256=ref, returned_sha256=digest(raw)))
    write(lab.artifacts / ("sql-api-" + source + "-" + ident + ".json"), dict(mapping=rows, api=stable(values), attachments=evidence))
    return values


def no_publication(lab, data):
    lab.check("collect/serve do not implicitly publish", lab.pointer() is None)
    for target in ("/sources", path(data.entries["dd-001"]), "/attachments/made-up"):
        lab.error(target, "snapshot_not_published", 503)
    lab.publish(data.empty)
    status, _, value, _, _ = lab.request("/sources")
    lab.check("published zero-source snapshot returns empty catalog", status == 200 and value == [])
    lab.error(path(data.entries["dd-001"]), "source_not_found", 404)
    lab.publish(data.empty)
    lab.check("empty publication is idempotent", lab.pointer() == data.empty and len(lab.audit()) == 1)


def lifecycle(lab, data):
    data.a = lab.create("A")
    a_mapping = lab.mapping(data.a)
    lab.check("create populated A leaves published empty snapshot", lab.pointer() == data.empty)
    status, _, values, _, _ = lab.request("/sources")
    lab.check("unpublished A changes no catalog", status == 200 and values == [])
    expected = dict(sources=sorted(data.entries), resources=len(data.initial) + 8)
    row = lab.sql("SELECT * FROM seal.snapshot WHERE id=%s", (data.a,))[0]
    lab.check("A persists sealed exact source/resource/version counts", row["sealed"]
              and row["source_count"] == len(expected["sources"])
              and row["resource_count"] == expected["resources"]
              and row["version_count"] == expected["resources"], snapshot=row)
    lab.publish(data.a)
    status, _, sources, _, _ = lab.request("/sources")
    # Existing /sources contract hides globally ambiguous entries, including whole empty rows.
    multiplicity = Counter(entry for s in a_mapping["sources"] for entry in s["entry_urls"])
    expected_sources = [dict(id=s["source_id"], entry_urls=[e for e in s["entry_urls"] if multiplicity[e] == 1])
                        for s in a_mapping["sources"] if any(multiplicity[e] == 1 for e in s["entry_urls"])]
    lab.check("published A catalog exactly equals frozen SQL entries", status == 200 and sources == expected_sources)
    data.a_native = stable(check_public(lab, data, data.a, "dd-001"))
    data.a_bulk = stable(check_public(lab, data, data.a, "dd-002"))
    data.a_attachment = stable(check_public(lab, data, data.a, "dd-013"))
    lab.error(path(data.entries["dd-004"]), "source_ambiguous", 409)
    status, _, values, _, _ = lab.request(path(data.entries["dd-003"]))
    lab.check("frozen source with no successful version returns empty result list", status == 200 and values == [])
    # Keep tokens alive across internal changes, creation, B publication, and rollback.
    _, headers, first, _, _ = lab.request(path(data.entries["dd-002"], limit=1))
    data.a_cursor_url = next_url(headers)
    data.a_first = first
    _, _, attached, _, _ = lab.request(path(data.entries["dd-013"]))
    data.a_attachment_url = attached[0]["attachments"][0]["url"]
    lab.origin.set("/api-index", html("Index Beta", "Changed published index Beta.", ("/article", "/api.pdf", "/added")))
    lab.origin.set("/article", html("Article Beta", "Changed real article Beta."))
    lab.origin.set("/added", html("New resource Beta", "Newly discovered resource only in Snapshot B."))
    config = data.config_path.read_text().replace("- /never", "- /never\n- /added")
    data.config_path.write_text(config)
    lab.cli("collect", data.config_path, expected=(0, 1))
    successful_after_change = data.current("dd-001")
    lab.check("real CLI committed changed and newly discovered success", len(successful_after_change) == 4
              and any(r["url"].endswith("/added") for r in successful_after_change))
    lab.origin.set("/article", b"Failure", "text/plain", status=500)
    lab.cli("recheck", data.config_path, expected=(0, 1))
    failed = lab.sql("SELECT o.outcome FROM seal.observation o JOIN seal.resource r ON r.id=o.resource_id "
                     "WHERE r.url=%s ORDER BY o.observed_at DESC LIMIT 1", (lab.origin.base + "/article",))
    lab.check("real failed recheck retains last successful internal version", failed and failed[0]["outcome"] != "parsed"
              and [(r["id"], r["result_ref"]) for r in data.current("dd-001")] == [(r["id"], r["result_ref"]) for r in successful_after_change])
    for n in (0, 6):
        data.success("dd-002", f"snapshot-{n:03}", data.envelope(f"Beta resource {n}"))
    data.success("dd-002", "snapshot-new", data.envelope("Beta new resource"))
    data.attached_b = dict(data.success("dd-013", "attached-result", data.envelope("Attachment Beta without original attachment")))
    old_config = lab.sql("SELECT config FROM seal.source WHERE id='dd-002'")[0]["config"]
    data.changed_entry = "https://fixture.example.org/dd-002-new-entry"
    lab.sql("UPDATE seal.source SET config=%s WHERE id='dd-002'", (Jsonb({**old_config, "entry_urls": [data.changed_entry]}),))
    data.source("dd-012")
    lab.check("A source and item SQL frozen after internal changes", lab.mapping(data.a) == a_mapping)
    lab.check("A native content unchanged after collect and failed recheck", stable(check_public(lab, data, data.a, "dd-001")) == data.a_native)
    lab.check("A bulk and attachment unchanged after internal successful versions", stable(check_public(lab, data, data.a, "dd-002")) == data.a_bulk
              and stable(check_public(lab, data, data.a, "dd-013")) == data.a_attachment)
    lab.error(path(data.changed_entry), "source_not_found", 404)
    status, _, sources, _, _ = lab.request("/sources")
    lab.check("new source and changed internal entry stay invisible under A", status == 200 and sources == expected_sources)
    data.b = lab.create("B")
    lab.check("create B does not publish", lab.pointer() == data.a)
    lab.publish(data.b)
    lab.error(path(data.entries["dd-002"]), "source_not_found", 404)
    data.b_native = stable(check_public(lab, data, data.b, "dd-001"))
    data.b_bulk = stable(check_public(lab, data, data.b, "dd-002", data.changed_entry))
    lab.check("B new requests contain new resource and correct changed versions", len(data.b_native) == 4
              and len(data.b_bulk) == 8 and data.b_bulk != data.a_bulk)
    lab.publish(data.a)
    lab.check("rollback A restores sources and versions", stable(check_public(lab, data, data.a, "dd-001")) == data.a_native
              and stable(check_public(lab, data, data.a, "dd-002")) == data.a_bulk)
    lab.check("rollback preserves internal newer versions", len(data.current("dd-001")) == 4
              and data.current("dd-013")[0]["id"] == data.attached_b["version"])


def tokens(lab, data):
    lab.publish(data.b)
    values = list(data.a_first)
    url = data.a_cursor_url
    for _ in range(10):
        if not url:
            break
        status, headers, part, _, _ = lab.request(url)
        lab.check("historical page continues after B publication", status == 200 and isinstance(part, list))
        if status != 200:
            break
        values.extend(part)
        url = next_url(headers)
    lab.check("historical A pagination has exact seven A values with no gaps or duplicates", stable(values) == data.a_bulk)
    status, _, _, raw, _ = lab.request(data.a_attachment_url)
    lab.check("old A attachment still matches original bytes after B publication", status == 200 and raw == data.attachment_bytes)
    lab.publish(data.a)
    status, _, _, raw, _ = lab.request(data.a_attachment_url)
    lab.check("old A attachment survives rollback", status == 200 and digest(raw) == data.attachment_key)
    cursor = cursor_from(data.a_cursor_url)
    lab.error(path(data.entries["dd-002"], cursor=tamper(cursor)), "invalid_cursor", 400)
    lab.error(path(data.entries["dd-002"], cursor=old_token(lab, cursor, 3601)), "invalid_cursor", 400)
    attachment = data.a_attachment_url.rsplit("/", 1)[1]
    lab.error("/attachments/" + tamper(attachment), "attachment_not_found", 404)
    lab.error("/attachments/" + old_token(lab, attachment, 86401), "attachment_not_found", 404)
    box = Fernet(lab.token_key.encode())
    original = box.decrypt(attachment.encode())
    substitutions = [(data.a, data.b, "snapshot"), (data.attached_a["version"], data.attached_b["version"], "version")]
    for before, after, label in substitutions:
        changed = original.replace(before.encode(), after.encode())
        lab.check("opaque attachment contains auditable " + label + " binding", changed != original)
        if changed != original:
            lab.error("/attachments/" + box.encrypt(changed).decode(), "attachment_not_found", 404)
    # The outer archive may bind attachments by an ordinal rather than carrying a raw hash.
    payload = json.loads(original)
    ordinals = [key for key, value in payload.items() if type(value) is int and value == 0]
    lab.check("attachment token offers one valid ordinal ownership mutation fixture", bool(ordinals))
    for key in ordinals:
        altered = {**payload, key: 999}
        lab.error("/attachments/" + box.encrypt(json.dumps(altered).encode()).decode(), "attachment_not_found", 404)


def permissions(lab, data):
    # Give all three callers equal permission before testing caller binding.
    original = copy.deepcopy(lab.service_config["callers"])
    equal = copy.deepcopy(original)
    for c in equal:
        c["sources"] = list(lab.allowed)
    lab.write_config(equal)
    lab.error(data.a_cursor_url, "invalid_cursor", 400, key=2)
    lab.error(data.a_attachment_url, "attachment_not_found", 404, key=2)
    revoked = copy.deepcopy(equal)
    revoked[0]["sources"] = [s for s in lab.allowed if s not in ("dd-002", "dd-013")]
    lab.write_config(revoked)
    status, _, sources, _, _ = lab.request("/sources")
    lab.check("permission revocation immediately filters frozen catalog without restart", status == 200
              and all(s["id"] not in ("dd-002", "dd-013") for s in sources))
    lab.error(path(data.entries["dd-002"]), "source_forbidden", 403)
    lab.error(data.a_cursor_url, "source_forbidden", 403)
    lab.error(data.a_attachment_url, "source_forbidden", 403)
    lab.write_config(equal)
    status, _, _, raw, _ = lab.request(data.a_attachment_url)
    lab.check("permission restoration immediately re-enables valid historical attachment", status == 200 and raw == data.attachment_bytes)
