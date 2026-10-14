"""Snapshot operator bounds, durable immutability, archive failures and no-write proof."""
from __future__ import annotations

import time

import psycopg
from issue23_contract import check_public, path
from issue23_lab import digest, write


def listing(lab, data):
    listed, after, pages = [], None, []
    for _ in range(30):
        args = ["snapshot", "list", "--limit", "1"]
        if after:
            args += ["--after", after]
        result = lab.json_cli(*args)
        page = result.get("snapshots", [])
        lab.check("CLI list is bounded and reports current publication", len(page) <= 1 and result.get("published_snapshot_id") == lab.pointer())
        listed.extend(r.get("snapshot_id", r.get("id")) for r in page)
        pages.append(result)
        after = result.get("next_after")
        if not after:
            break
    expected = [r["id"] for r in lab.sql("SELECT id FROM seal.snapshot")]
    lab.check("CLI paginated list covers each existing snapshot once", len(listed) == len(set(listed)) and set(listed) == set(expected))
    def walk(field, option, next_field):
        output, after = [], None
        for _ in range(30):
            args = ["snapshot", "inspect", data.a, "--limit", "1"]
            if after:
                args += [option, after]
            result = lab.json_cli(*args)
            lab.check("CLI inspect bounds mappings and sources and exposes frozen counts", len(result.get("items", [])) <= 1
                      and len(result.get("sources", [])) <= 1 and result.get("published_snapshot_id") == lab.pointer())
            output.extend(result.get(field, []))
            after = result.get(next_field)
            if not after:
                break
        return output
    items = walk("items", "--after", "next_after")
    sources = walk("sources", "--source-after", "source_next_after")
    mapping = lab.mapping(data.a)
    lab.check("CLI inspect keyset walks frozen mapping without duplicates", len(items) == len(mapping["items"])
              and {r["resource_id"] for r in items} == {r["resource_id"] for r in mapping["items"]})
    lab.check("CLI inspect source keyset walks frozen source metadata", len(sources) == len(mapping["sources"])
              and {r["source_id"] for r in sources} == {r["source_id"] for r in mapping["sources"]})
    for verb in ("list", "inspect"):
        for limit in ("0", "101", "-1", "bad"):
            args = ["snapshot", verb] + ([data.a] if verb == "inspect" else []) + ["--limit", limit]
            result = lab.cli(*args, expected=(1, 2))
            lab.check("CLI invalid page limit is explicit and safe", result.returncode != 0 and "Traceback" not in result.stderr)
    before = lab.pointer()
    result = lab.cli("snapshot", "inspect", "00000000-0000-0000-0000-000000000000", expected=(1, 2))
    lab.check("inspect nonexistent snapshot is explicit and leaves publication", result.returncode != 0 and lab.pointer() == before)
    write(lab.artifacts / "cli-snapshot-list.json", pages)
    write(lab.artifacts / "cli-snapshot-inspect.json", dict(items=items, sources=sources))


def immutable(lab, data):
    before = lab.mapping(data.a)
    mutations = [
        ("source metadata", "UPDATE seal.snapshot_source SET entry_urls='[\"https://tampered.example.org\"]' WHERE snapshot_id=%s AND source_id='dd-002'", (data.a,)),
        ("item version", "UPDATE seal.snapshot_item SET version_id=%s WHERE snapshot_id=%s AND resource_id='snapshot-000'", (data.seeded["snapshot-000"]["version"], data.a)),
        ("item deletion", "DELETE FROM seal.snapshot_item WHERE snapshot_id=%s AND resource_id='snapshot-000'", (data.a,)),
        ("source deletion", "DELETE FROM seal.snapshot_source WHERE snapshot_id=%s AND source_id='dd-003'", (data.a,)),
        ("snapshot statistics", "UPDATE seal.snapshot SET resource_count=resource_count+1 WHERE id=%s", (data.a,)),
        ("item insertion", "INSERT INTO seal.snapshot_item(snapshot_id,source_id,frontier_id,resource_id,version_id,url) "
         "SELECT %s,'dd-002','frontier-dd-002',r.id,r.current_version_id,r.url FROM seal.resource r WHERE r.id='snapshot-new'", (data.a,))]
    for label, query, params in mutations:
        rejected, code = False, None
        try:
            with psycopg.connect(lab.dsn) as conn:
                conn.execute(query, params)
        except psycopg.Error as exc:
            rejected, code = True, exc.sqlstate
        lab.check("sealed snapshot rejects persistent " + label + " mutation", rejected, sqlstate=code)
    lab.check("rejected mutations leave original frozen map intact", lab.mapping(data.a) == before)


def archives(lab, data):
    ref = data.alpha[0]["ref"]
    original = lab.body(ref)
    lab.publish(data.a)
    for label, code, raw in (("corrupt", "archive_corrupt", b"different bytes at SHA"),
                            ("missing", "archive_missing", None)):
        if raw is None:
            lab.s3.delete_object(Bucket=lab.bucket, Key=ref)
        else:
            lab.s3.put_object(Bucket=lab.bucket, Key=ref, Body=raw)
        lab.error(path(data.entries["dd-002"]), code, 500)
        lab.check("archive fault does not fall back to internal Beta version", lab.pointer() == data.a)
        lab.publish(data.b)
        audit = lab.audit()
        result = lab.cli("snapshot", "publish", data.a, expected=(1, 2))
        lab.check("publish validates " + label + " archive and preserves old pointer/audit", result.returncode != 0
                  and lab.pointer() == data.b and lab.audit() == audit and "Traceback" not in result.stderr)
        lab.s3.put_object(Bucket=lab.bucket, Key=ref, Body=original)
        lab.publish(data.a)
    attachment_original = lab.body(data.attachment_key)
    lab.s3.put_object(Bucket=lab.bucket, Key=data.attachment_key, Body=b"corrupt archived attachment")
    lab.error(data.a_attachment_url, "archive_corrupt", 500)
    lab.publish(data.b)
    audit = lab.audit()
    result = lab.cli("snapshot", "publish", data.a, expected=(1, 2))
    lab.check("publish validates original attachment hashes and keeps pointer on failure", result.returncode != 0
              and lab.pointer() == data.b and lab.audit() == audit)
    lab.s3.put_object(Bucket=lab.bucket, Key=data.attachment_key, Body=attachment_original)
    try:
        lab.docker("stop", "--time", "1", lab.containers[1])
        lab.error(path(data.entries["dd-001"]), "object_storage_unavailable", 503)
        audit = lab.audit()
        result = lab.cli("snapshot", "publish", data.a, expected=(1, 2), timeout=35)
        lab.check("unavailable S3 publication fails without switching pointer", result.returncode != 0
                  and lab.pointer() == data.b and lab.audit() == audit)
    finally:
        lab.docker("start", lab.containers[1])
        deadline = time.monotonic() + 20
        while True:
            try:
                lab.s3.list_buckets()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.2)
    lab.publish(data.a)
    check_public(lab, data, data.a, "dd-002")
    status, _, _, raw, _ = lab.request(data.a_attachment_url)
    lab.check("restored archive returns same original attachment hash", status == 200 and digest(raw) == data.attachment_key)


def readonly(lab, data):
    before = lab.snapshot()
    requests_before = len(lab.origin.requests)
    lab.request("/sources")
    check_public(lab, data, data.a, "dd-001")
    lab.request(data.a_cursor_url)
    lab.request(data.a_attachment_url)
    lab.error(path("https://unknown.example.org/entry"), "source_not_found", 404)
    lab.check("HTTP sources/results/cursor/attachment/errors never write SQL or S3", before == lab.snapshot())
    lab.check("API never invokes source network or collection", requests_before == len(lab.origin.requests))
    write(lab.artifacts / "read-only-database-s3-digests.json", before)
