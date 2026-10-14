"""Native collector fixture plus explicit synthetic storage boundaries for HTTP acceptance."""
from __future__ import annotations

import copy
import json
import uuid

import psycopg
import yaml
from issue20_fixture import html, pdf
from issue22_lab import digest, write
from psycopg.types.json import Jsonb


def content(label="Alpha"):
    rows = "".join(f"<tr><td>Company {i}</td><td>{i * 120}</td><td>2026-10-{i + 1:02}</td></tr>" for i in range(8))
    return ("<!doctype html><html><head><title>SDK content " + label + "</title>"
        '<meta name="author" content="Fixture Author"><meta name="description" content="Authorized content">'
        '<meta name="keywords" content="cases,evidence"></head><body><main><h1>SDK ' + label + "</h1>"
        '<p>The authorized source publishes documented corporate findings with images, video, audio and tabular records. '
        'This paragraph gives actual content context for media extraction and preserves original links and formatting.</p>'
        '<figure><img src="/evidence.png" alt="Detailed corporate evidence figure" width="640" height="480">'
        '<figcaption>A public chart explains the financial amounts and dated evidence.</figcaption></figure>'
        '<video src="/evidence.mp4" controls><source src="/alternate.mp4" type="video/mp4"></video>'
        '<audio src="/recording.mp3" controls><source src="/alternate.mp3" type="audio/mpeg"></audio>'
        '<table><caption>Corporate records</caption><thead><tr><th>Company</th><th>Amount</th><th>Date</th></tr></thead><tbody>'
        + rows + '</tbody></table><p>All records are synthetic authorized acceptance data.</p>'
        '<a href="/article">Deep article</a><a href="/api.pdf">Downloaded PDF</a>'
        '<a href="/never">Not successful</a><a href="/undownloaded.xlsx">File link only</a>'
        '<a href="https://external.example.net/reference" title="Reference">External source</a>'
        '</main></body></html>').encode()


class Data:
    def __init__(self, lab):
        self.lab = lab
        self.entries = {}
        self.seeded = {}
        self.allowed = []
        self.lab.allowed = self.allowed
        self.lab.source_entries = self.entries

    def collect_initial(self):
        lab = self.lab
        lab.origin.set("/api-index", content())
        lab.origin.set("/article", html("First article", "Initial article Alpha content with original markdown."))
        lab.origin.set("/api.pdf", pdf("Issue22 authentic original PDF bytes."), "application/pdf")
        lab.origin.set("/never", b"Never successful", status="network")
        lab.origin.set("/undownloaded.xlsx", b"Not archived a file link alone", status=404)
        config = dict(id="dd-001", entry_urls=[lab.origin.base + "/api-index"],
            allowed_hosts=["127.0.0.1"], allowed_path_prefixes=["/api-index", "/article", "/api.pdf", "/never"],
            archive_approved=True, scope="Issue22 independent authorized synthetic HTTP source",
            robots=False, recheck_seconds=0, concurrency=1, delay=0.,
            budget=dict(requests=100, seconds=90, response_bytes=1048576),
            crawl4ai=dict(mode="http", max_depth=1, word_count_threshold=1))
        self.config_path = lab.work / "dd-001.yaml"
        self.config_path.write_text(yaml.safe_dump(config))
        (lab.artifacts / "dd-001.yaml").write_text(self.config_path.read_text())
        self.entries["dd-001"] = config["entry_urls"][0]
        self.allowed.append("dd-001")
        lab.cli("collect", self.config_path, expected=(0, 1))
        self.initial = self.current("dd-001")
        lab.check("native collector fixture has entry/deep/PDF successes and excludes never-successful",
            {v["url"].rsplit("/", 1)[1] for v in self.initial} == {"api-index", "article", "api.pdf"},
            urls=[v["url"] for v in self.initial])
        if not self.initial:
            raise RuntimeError("Native collector produced no successful input")
        self.native = json.loads(lab.body(next(v["result_ref"] for v in self.initial if v["url"].endswith("api-index"))))
        write(lab.artifacts / "native-sdk-index.json", self.native)
        sdk = self.native["result"]
        lab.check("real SDK emitted nonempty images/videos/audios/table content",
                  all(sdk.get("media", {}).get(group) for group in ("images", "videos", "audios"))
                  and bool(sdk.get("tables")), collections={k: len(v) for k, v in sdk.get("media", {}).items()},
                  tables=len(sdk.get("tables", [])))

    def current(self, source):
        return self.lab.sql("SELECT r.id,r.url,v.* FROM seal.resource r JOIN seal.frontier f ON f.id=r.frontier_id "
            "JOIN seal.version v ON v.id=r.current_version_id WHERE f.source_id=%s ORDER BY r.id COLLATE \"C\"", (source,))

    def source(self, ident, entry=None):
        entry = entry or f"https://fixture.example.org/{ident}"
        config = dict(id=ident, entry_urls=[entry], archive_approved=True,
                      scope="Synthetic SQL storage boundaries for authorized API acceptance")
        frontier, run = "frontier-" + ident, "run-" + ident
        self.lab.sql("INSERT INTO seal.source(id,config) VALUES(%s,%s)", (ident, Jsonb(config)))
        self.lab.sql("INSERT INTO seal.frontier(id,source_id) VALUES(%s,%s)", (frontier, ident))
        self.lab.sql("INSERT INTO seal.run(id,source_id,mode,generation,run_seq,deadline,status,seeds,config) "
                     "VALUES(%s,%s,'collect',0,1,now()+interval '1hour','complete','[]',%s)", (run, ident, Jsonb(config)))
        self.entries[ident] = entry
        self.allowed.append(ident)
        return ident

    def envelope(self, text, **kwargs):
        value = copy.deepcopy(self.native)
        value["result"]["markdown"] = dict(raw_markdown=text,
            markdown_with_citations="PRIVATE_CITATION", references_markdown="PRIVATE_REFERENCE",
            fit_markdown="PRIVATE_FIT", fit_html="PRIVATE_FIT_HTML")
        value["result"]["metadata"].update(depth=93, parent_url="PRIVATE_PARENT", internal="PRIVATE_METADATA")
        value["result"]["html"] = "PRIVATE_HTML"
        value["result"]["response_headers"] = {"x-private": "PRIVATE_HEADER"}
        value["result"]["downloaded_files"] = None
        value["attachment_refs"] = []
        value.update(kwargs)
        return value

    def success(self, source, rid, envelope, result_ref=None, url=None):
        lab = self.lab
        url = url or f"https://fixture.example.org/{source}/resource/{rid}"
        raw = json.dumps(envelope, sort_keys=True, ensure_ascii=False).encode()
        ref = result_ref or lab.put(raw)
        oid, vid = str(uuid.uuid4()), str(uuid.uuid4())
        fingerprint = digest((source + rid + ref).encode())
        with psycopg.connect(lab.dsn) as conn:
            conn.execute("INSERT INTO seal.resource(id,frontier_id,url) VALUES(%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                         (rid, "frontier-" + source, url))
            sequence = conn.execute("SELECT coalesce(max(sequence),0)+1 FROM seal.version WHERE resource_id=%s", (rid,)).fetchone()[0]
            conn.execute("INSERT INTO seal.observation(id,resource_id,run_id,attempt_epoch,delivery_key,url,body_hash,"
                "result_ref,input_mode,outcome,version_id) VALUES(%s,%s,%s,1,%s,%s,%s,%s,'http','parsed',%s)",
                (oid, rid, "run-" + source, oid, url, fingerprint, ref, vid))
            conn.execute("SELECT pg_advisory_xact_lock(72032102)")
            conn.execute("INSERT INTO seal.version(id,resource_id,sequence,observation_id,body_hash,input_mode,"
                "result_ref,framework_version,parser_config) VALUES(%s,%s,%s,%s,%s,'http',%s,'0.9.4','{}')",
                (vid, rid, sequence, oid, fingerprint, ref))
            conn.execute("UPDATE seal.resource SET current_version_id=%s,last_outcome='parsed',last_checked_at=now() WHERE id=%s", (vid, rid))
        self.seeded[rid] = dict(source=source, url=url, ref=ref, raw=raw, version=vid, envelope=envelope)
        return self.seeded[rid]

    def setup_storage_boundaries(self):
        lab = self.lab
        self.source("dd-002")
        for n in range(105):
            self.success("dd-002", f"bulk-{n:03}", self.envelope(f"Snapshot Alpha resource {n}"))
        self.source("dd-003")
        self.source("dd-004", "https://fixture.example.org/ambiguous")
        self.source("dd-005", self.entries["dd-004"])
        for ident in ("dd-006", "dd-007", "dd-008", "dd-009", "dd-010", "dd-011", "dd-012", "dd-013", "dd-014", "dd-015"):
            self.source(ident)
        self.success("dd-006", "missing-result", self.envelope("Missing object"), result_ref="1" * 64)
        corrupt = self.success("dd-007", "corrupt-result", self.envelope("Corrupted object"))
        lab.s3.put_object(Bucket=lab.bucket, Key=corrupt["ref"], Body=b"different bytes at expected SHA256")
        non_json_ref = lab.put(b"not a JSON archive")
        self.success("dd-008", "nonjson-result", self.envelope("dummy"), result_ref=non_json_ref)
        bad = self.envelope("Wrong type metadata")
        bad["result"]["metadata"]["title"] = 7
        self.success("dd-009", "typed-result", bad)
        bad_ref = self.envelope("Bad attachment reference", attachment_refs=["not-sha256"])
        self.success("dd-010", "bad-reference", bad_ref)
        empty = self.envelope("empty")
        empty["result"].update(markdown=None, metadata={}, media={}, links={}, tables=[])
        self.success("dd-011", "empty-fields", empty)
        self.success("dd-012", "same-host", self.envelope("Separate entry shares host"))
        attachment = b"Issue22 original archive attachment\x00\xff\x01" * 80
        self.attachment_key = lab.put(attachment)
        self.attachment_bytes = attachment
        self.success("dd-013", "attached-result", self.envelope("Archived attachment source", attachment_refs=[self.attachment_key]))
        self.success("dd-014", "huge-result", self.envelope("Large content" * 250))
        write(lab.artifacts / "fixtures.json", dict(
            entries=self.entries, bulk_resources=105, expected_order=[f"bulk-{n:03}" for n in range(105)],
            native_framework="crawl4ai 0.9.4 CLI real HTTP and PDF", attachment=dict(
                bytes=len(attachment), sha256=digest(attachment)),
            storage_faults="Missing SHA object; wrong bytes at SHA; invalid JSON/type/reference; no mocks"))


def expected_content(row, envelope):
    result = envelope["result"]
    def scalar(data, fields):
        return {k: data.get(k) for k in fields}
    media = {}
    for group in ("images", "videos", "audios"):
        media[group] = []
        for item in (result.get("media") or {}).get(group, []):
            public = scalar(item, ("src", "alt", "desc", "type", "format", "width"))
            if isinstance(public["width"], str):
                public["width"] = int(public["width"])
            media[group].append(public)
    return dict(url=row["url"], markdown=(result.get("markdown") or {}).get("raw_markdown", ""),
        metadata=scalar(result.get("metadata") or {}, ("title", "author", "description", "keywords")),
        links={g: [scalar(i, ("href", "text", "title")) for i in (result.get("links") or {}).get(g, [])]
               for g in ("internal", "external")}, media=media,
        tables=[scalar(i, ("headers", "rows", "caption", "summary")) for i in result.get("tables", [])])
