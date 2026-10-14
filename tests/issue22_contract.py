"""Public HTTP behavior assertions based only on Issue22 and its published Plan."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.fernet import Fernet
from issue20_fixture import html
from issue22_data import expected_content
from issue22_lab import digest, write
from psycopg.types.json import Jsonb


def results(data, source, **options):
    return "/results?" + urlencode(dict(source_url=data.entries[source], **options))


def next_url(headers):
    value = headers.get("Link", "")
    match = re.fullmatch(r'<([^>]+)>; rel="next"', value)
    return match.group(1) if match else None


def tamper(token):
    middle = len(token) // 2
    return token[:middle] + ("A" if token[middle] != "A" else "B") + token[middle + 1:]


def old_token(lab, token, age):
    """Reissue authentic same payload at expired time; replace any bound epoch without naming fields."""
    box = Fernet(lab.token_key.encode())
    issued = box.extract_timestamp(token.encode())
    raw = box.decrypt(token.encode())
    try:
        value = json.loads(raw)
        def shift(node):
            if isinstance(node, dict):
                return {k: shift(v) for k, v in node.items()}
            if isinstance(node, list):
                return [shift(v) for v in node]
            if isinstance(node, (int, float)) and abs(node - issued) < 60:
                return node - age
            return node
        raw = json.dumps(shift(value), separators=(",", ":")).encode()
    except ValueError:
        pass
    return box.encrypt_at_time(raw, issued - age).decode()


def check_native(lab, data):
    status, headers, items, _, _ = lab.request(results(data, "dd-001"),
        headers={"Host": "attacker.example.net", "X-Forwarded-Host": "attacker.example.net",
                 "X-Forwarded-Proto": "https"})
    lab.check("HTTP returns direct native current-success JSON array", status == 200 and isinstance(items, list))
    if not isinstance(items, list):
        return
    rows = data.current("dd-001")
    lab.check("entry and deep successful resources included; never-successful excluded",
              [i["url"] for i in items] == [r["url"] for r in rows] and len(items) == 3)
    for row, item in zip(rows, items, strict=True):
        envelope = json.loads(lab.body(row["result_ref"]))
        expected = expected_content(row, envelope)
        actual = {k: v for k, v in item.items() if k != "attachments"}
        lab.check("SDK archive field values/strict whitelist " + row["url"].rsplit("/", 1)[1],
                  actual == expected and set(item) == set(expected) | {"attachments"})
        attachments = item.get("attachments", [])
        lab.check("outer archive refs alone determine attachment count", len(attachments) == len(envelope["attachment_refs"]))
        for attachment, ref in zip(attachments, envelope["attachment_refs"], strict=True):
            lab.check("absolute safe download URL ignores spoofed Host", attachment["url"].startswith(lab.base + "/attachments/")
                      and attachment == dict(url=attachment["url"], name=None, media_type=None))
            download_status, _, _, original, _ = lab.request(attachment["url"])
            lab.check("actual SDK downloaded PDF returns archived original bytes", download_status == 200
                      and original == lab.body(ref) and digest(original) == ref)
    index = next((i for i in items if i["url"].endswith("api-index")), None)
    lab.check("undownloaded spreadsheet link stays in markdown/links and never masquerades as attachment",
              bool(index) and "undownloaded.xlsx" in index["markdown"] and not index["attachments"])
    lab.check("JSON is no-store", headers.get("Cache-Control") == "no-store")
    _, _, normalized, _, _ = lab.request("/results?" + urlencode(dict(source_url=data.entries["dd-001"] + "#ignored")))
    lab.check("entry matching uses existing URL normalization", isinstance(normalized, list) and [i["url"] for i in normalized] == [i["url"] for i in items])
    st, _, empty, _, _ = lab.request(results(data, "dd-011"))
    expected_empty = dict(url=data.seeded["empty-fields"]["url"], markdown="", metadata=dict.fromkeys(
        ("title", "author", "description", "keywords")), links=dict(internal=[], external=[]),
        media=dict(images=[], videos=[], audios=[]), tables=[], attachments=[])
    lab.check("missing content becomes null scalars/empty collections and empty markdown", st == 200 and empty == [expected_empty])


def authentication(lab, data):
    path = results(data, "dd-001")
    for headers in ({}, {"Authorization": "Bearer invalid-key"}, {"Authorization": "Basic abc"}):
        response_headers = lab.error(path, "unauthenticated", 401, key=None, headers=headers)
        lab.check("401 advertises Bearer authentication", response_headers.get("WWW-Authenticate") == "Bearer")
    lab.error("/results?limit=bad", "unauthenticated", 401, key=None)
    lab.error(results(data, "dd-012"), "source_forbidden", 403, key=1)
    lab.error(results(data, "dd-003"), "source_forbidden", 403, key=1)
    lab.error("/results?" + urlencode(dict(source_url="https://unknown.example.org/none")), "source_not_found", 404)
    lab.error(results(data, "dd-004"), "source_ambiguous", 409)
    st, _, empty, _, _ = lab.request(results(data, "dd-003"))
    lab.check("registered empty source returns empty array", st == 200 and empty == [])
    st, _, separate, _, _ = lab.request(results(data, "dd-012"))
    lab.check("same host different entry remains separate source", st == 200 and len(separate) == 1
              and separate[0]["markdown"] == "Separate entry shares host")
    for suffix in ("", "?source_url=", "?source_url=not-a-url", "?limit=1", "?source_url=" + data.entries["dd-001"] + "&source_url=duplicate"):
        lab.error("/results" + suffix, "invalid_parameters", 400)
    for value in ("0", "101", "-1", "1.5", "bad", ""):
        lab.error(results(data, "dd-001", limit=value), "invalid_parameters", 400)
    lab.error(results(data, "dd-001", cursor="made-up"), "invalid_cursor", 400)
    lab.error("/not-a-route", "not_found", 404)
    lab.error(path, "method_not_allowed", 405, method="POST")


def timeline(lab, data):
    def query_article():
        st, _, values, _, _ = lab.request(results(data, "dd-001"))
        return next((i for i in values if i["url"].endswith("/article")), None) if st == 200 else None
    alpha = query_article()
    before = data.current("dd-001")
    lab.origin.set("/article", html("Changed article", "Updated article Beta content with original markdown."))
    lab.cli("collect", data.config_path, expected=(0, 1))
    beta = query_article()
    changed = data.current("dd-001")
    lab.check("first success then changed content creates visible new current successful version",
              alpha and beta and "Alpha" in alpha["markdown"] and "Beta" in beta["markdown"]
              and next(r for r in changed if r["url"].endswith("/article"))["sequence"] == 2)
    lab.cli("collect", data.config_path, expected=(0, 1))
    unchanged = query_article()
    repeated = data.current("dd-001")
    lab.check("unchanged check does not advance successful version or alter returned content", unchanged == beta
              and [(r["id"], r["sequence"], r["result_ref"]) for r in repeated] == [(r["id"], r["sequence"], r["result_ref"]) for r in changed])
    lab.origin.set("/article", b"Upstream failure with private details", "text/plain", status=500)
    lab.cli("collect", data.config_path, expected=(0, 1))
    failed = query_article()
    observations = lab.sql("SELECT o.outcome FROM seal.observation o JOIN seal.resource r ON r.id=o.resource_id "
        "WHERE r.url=%s ORDER BY o.observed_at", (lab.origin.base + "/article",))
    lab.check("latest failed observation retains existing successful version", failed == beta and observations[-1]["outcome"] != "parsed",
              observed_outcomes=[r["outcome"] for r in observations])
    lab.check("initial source was actual first-success sequence", all(r["sequence"] == 1 for r in before))


def pagination(lab, data):
    st, headers, first, _, _ = lab.request(results(data, "dd-002", limit=1))
    cursor_url = next_url(headers)
    lab.check("limit1 array has absolute next-page Link", st == 200 and len(first) == 1 and bool(cursor_url)
              and cursor_url.startswith(lab.base + "/results?"))
    if not cursor_url:
        return
    cursor = parse_qs(urlsplit(cursor_url).query)["cursor"][0]
    lab.check("pagination cursor authenticated encrypted and hides resource IDs/hashes", cursor.startswith("gAAAA")
              and "bulk-" not in cursor and data.seeded["bulk-000"]["ref"] not in cursor)
    lab.error(results(data, "dd-012", cursor=cursor), "invalid_cursor", 400)
    lab.error(results(data, "dd-002", cursor=tamper(cursor)), "invalid_cursor", 400)
    lab.error(results(data, "dd-002", cursor=old_token(lab, cursor, 3601)), "invalid_cursor", 400)
    # A second caller permitted on dd001 still cannot use caller0's dd001 cursor.
    _, h1, _, _, _ = lab.request(results(data, "dd-001", limit=1))
    c1 = parse_qs(urlsplit(next_url(h1)).query)["cursor"][0]
    lab.error(results(data, "dd-001", cursor=c1), "invalid_cursor", 400, key=2)
    self_first = data.seeded["bulk-000"]
    data.success("dd-002", "bulk-000", data.envelope("Snapshot Beta already returned resource"))
    data.success("dd-002", "bulk-104", data.envelope("Snapshot Beta not yet returned resource"))
    data.success("dd-002", "bulk--new", data.envelope("New resource before first position"))
    data.success("dd-002", "bulk-999", data.envelope("New resource after last position"))
    all_items = list(first)
    url = cursor_url
    pages = 1
    while url:
        query = parse_qs(urlsplit(url).query)
        query["limit"] = [str((17, 5, 100)[pages % 3])]
        url = lab.base + "/results?" + urlencode({k: v[0] for k, v in query.items()})
        status, hs, values, _, _ = lab.request(url)
        if status != 200 or not isinstance(values, list):
            lab.check("all snapshot pages return successful arrays", False, actual=status)
            break
        all_items.extend(values)
        pages += 1
        url = next_url(hs)
        if pages > 120:
            raise RuntimeError("Pagination did not finish within bounded expected pages")
    expected_urls = [f"https://fixture.example.org/dd-002/resource/bulk-{n:03}" for n in range(105)]
    lab.check("cross-page committed changes/new resources retain initial105 snapshot without gaps/duplicates",
              [i["url"] for i in all_items] == expected_urls and all(i["markdown"] == f"Snapshot Alpha resource {n}" for n, i in enumerate(all_items)), pages=pages, count=len(all_items))
    status, h, values, _, _ = lab.request(results(data, "dd-002", limit=100))
    url = next_url(h)
    if url:
        status2, end_headers, remainder, _, _ = lab.request(url)
        values.extend(remainder if isinstance(remainder, list) else [])
        lab.check("last page omits Link", status2 == 200 and "Link" not in end_headers)
    lab.check("new first page observes107 successful current resources and Beta updates", status == 200 and len(values) == 107
        and next(i for i in values if i["url"] == self_first["url"])["markdown"] == "Snapshot Beta already returned resource")
    _, default_h, default, _, _ = lab.request(results(data, "dd-002"))
    lab.check("default limit20 exposes next Link instead of truncating", len(default) == 20 and bool(next_url(default_h)))
    write(lab.artifacts / "snapshot-public.json", all_items)


def attachments(lab, data):
    st, _, values, _, _ = lab.request(results(data, "dd-013"))
    if st != 200 or not values:
        lab.check("synthetic authentic archived attachment result available", False, status=st)
        return
    attachment = values[0]["attachments"][0]
    url = attachment["url"]
    token = url.rsplit("/", 1)[1]
    data.attachment_url = url
    status, headers, _, body, _ = lab.request(url)
    lab.check("attachment original bytes and SHA256 match archived S3 input", status == 200 and body == data.attachment_bytes
              and digest(body) == data.attachment_key)
    lab.check("attachment uses safe generic headers and no-store", headers.get("Content-Type") == "application/octet-stream"
        and headers.get("Content-Disposition") == 'attachment; filename="attachment"'
        and headers.get("X-Content-Type-Options") == "nosniff" and headers.get("Cache-Control") == "no-store")
    lab.check("attachment ID is encrypted and does not expose SQL Version or S3 hash", token.startswith("gAAAA")
        and data.attachment_key not in token and data.seeded["attached-result"]["version"] not in token)
    lab.error(url, "source_forbidden", 403, key=1)
    lab.error(url, "unauthenticated", 401, key=None)
    for invalid in ("invented", tamper(token), old_token(lab, token, 86401)):
        lab.error("/attachments/" + invalid, "attachment_not_found", 404)
    _, page_headers, _, _, _ = lab.request(results(data, "dd-001", limit=1))
    cursor = parse_qs(urlsplit(next_url(page_headers)).query)["cursor"][0]
    lab.error("/attachments/" + cursor, "attachment_not_found", 404)
    lab.error(results(data, "dd-013", cursor=token), "invalid_cursor", 400)
    # An authenticated token with Source replaced must still fail SQL Version ownership.
    box = Fernet(lab.token_key.encode())
    plain = box.decrypt(token.encode())
    replaced = plain.replace(b"dd-013", b"dd-012")
    if replaced != plain:
        forged_owner = box.encrypt(replaced).decode()
        lab.error("/attachments/" + forged_owner, "attachment_not_found", 404)
    else:
        lab.check("configured-key ownership mutation fixture identifies Source binding", False)
    data.success("dd-013", "attached-result", data.envelope("Current changed content without attachment"))
    status, _, _, body, _ = lab.request(url)
    lab.check("old valid attachment remains downloadable after current Version changes", status == 200 and body == data.attachment_bytes)
    # Reissue a fresh URL for faults after service restarts (public URL is configured per process).
    data.old_attachment_token = token


def revalidation(lab, data):
    """Observe original cursor lifetime and current configuration/authorization per request."""
    _, first_headers, _, _, _ = lab.request(results(data, "dd-001", limit=1))
    cursor = parse_qs(urlsplit(next_url(first_headers)).query)["cursor"][0]
    close_to_expiry = old_token(lab, cursor, 3595)
    status, page_headers, page_items, _, _ = lab.request(results(data, "dd-001", limit=1, cursor=close_to_expiry))
    following = next_url(page_headers)
    lab.check("authentic cursor within original one-hour limit accepts next page", status == 200 and len(page_items) == 1 and bool(following))
    if following:
        time.sleep(6)
        lab.error(following, "invalid_cursor", 400)
        lab.check("turning a page does not renew the original3600second cursor lifetime", lab.checks[-1]["status"] == "PASS")
    original = lab.sql("SELECT config FROM seal.source WHERE id='dd-001'")[0]["config"]
    changed = {**original, "entry_urls": [lab.origin.base + "/reconfigured-entry"]}
    lab.sql("UPDATE seal.source SET config=%s WHERE id='dd-001'", (Jsonb(changed),))
    try:
        lab.error(results(data, "dd-001", cursor=cursor), "source_not_found", 404)
        status, _, values, _, _ = lab.request("/results?" + urlencode(dict(source_url=changed["entry_urls"][0], cursor=cursor)))
        lab.check("cursor resolves presently registered same Source entry after configuration change", status == 200 and isinstance(values, list))
    finally:
        lab.sql("UPDATE seal.source SET config=%s WHERE id='dd-001'", (Jsonb(original),))
    revoked = [dict(id="all", key_sha256=digest(lab.keys[0].encode()),
                   sources=[s for s in lab.allowed if s not in ("dd-001", "dd-013")])]
    lab.start_service(callers=revoked)
    lab.error(results(data, "dd-001", cursor=cursor), "source_forbidden", 403)
    lab.error("/attachments/" + data.old_attachment_token, "source_forbidden", 403)
    lab.start_service()
