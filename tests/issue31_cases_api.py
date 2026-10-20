"""Independent Issue 31 business scenarios; product access uses CLI, SQL, S3 and HTTP."""
# ruff: noqa: F403,F405
from issue31_common import *

def test_api_before_publish_503(lab):
    """/results answers 503 before any snapshot is published."""
    api = Api(lab, {"all": ["dd-017", "dd-033", "dd-040", "dd-041", "dd-176", "dd-050", "dd-051", "dd-061"],
                    "limited": ["dd-033"], "single_duplicate": ["dd-061"]}).start()
    S["api"] = api
    resp = api.get("/results", "all")
    lab.check("503 snapshot_not_published", resp.status_code == 503 and "snapshot_not_published" in resp.text, [resp.status_code, resp.text[:300]])


def test_snapshot_create_and_publish(lab):
    """seal snapshot create/publish seals a closure of deliverable files."""
    result, receipt = lab.cli_json("snapshot", "create", label="snapshot create #1")
    lab.check("snapshot create exit 0", result.returncode == 0, result.stdout[-1500:] + result.stderr[-1500:])
    sid = receipt.get("snapshot_id") or receipt.get("id")
    lab.check("snapshot sealed", receipt.get("sealed") is True, receipt)
    S["snap1"] = sid
    files = {r["sha256"]: r for r in lab.q("SELECT * FROM seal.snapshot_file WHERE snapshot_id=%s", (sid,))}
    rows = lab.q("""SELECT dr.*, si.source_id, v.inputs_ref, v.result_ref FROM seal.snapshot_item si JOIN seal.delivery_row dr ON dr.version_id=si.version_id
                    JOIN seal.version v ON v.id=si.version_id WHERE si.snapshot_id=%s""", (sid,))
    expected = {r["file_hash"] for r in rows} | {r["markdown_hash"] for r in rows}
    lab.check("snapshot_file closure == {文件哈希} ∪ {markdown_hash}", set(files) == expected,
              {"extra": sorted(set(files) - expected)[:5], "missing": sorted(expected - set(files))[:5]})
    internal = {r["inputs_ref"] for r in rows} | {r["result_ref"] for r in rows}
    lab.check("closure excludes inputs manifests / unified documents", not (internal & set(files)), sorted(internal & set(files))[:5])
    result, receipt = lab.cli_json("snapshot", "publish", sid, label="snapshot publish #1")
    lab.check("snapshot publish exit 0", result.returncode == 0, result.stdout[-1000:] + result.stderr[-1000:])
    pub = lab.q("SELECT published_snapshot_id FROM seal.publication")
    lab.check("publication pointer set", pub and str(pub[0]["published_snapshot_id"]) == str(sid), pub)
    S["snap1_rows"] = rows
    lab.save("evidence/snapshot1-rows.json", rows)


# ============================================================ API


def test_api_removed_endpoints(lab):
    """GET /sources and /attachments/* return 404 for everyone."""
    api = S["api"]
    out = {}
    for path in ("/sources", "/attachments/abc", "/attachments/gAAAAABexampletoken", "/results/legacy"):
        for caller in ("all", None):
            r = api.get(path, caller)
            out[f"{path}|{caller}"] = r.status_code
    lab.save("evidence/api-removed.json", out)
    lab.check("removed/unknown endpoints 404", all(v == 404 for v in out.values()), out)


def test_api_authentication(lab):
    """No / wrong bearer token is rejected (401)."""
    api = S["api"]
    r1, r2 = api.get("/results"), api.get("/results", raw_auth="Bearer wrong-token-value")
    lab.check("missing token 401", r1.status_code == 401, [r1.status_code, r1.text[:200]])
    lab.check("wrong token 401", r2.status_code == 401, [r2.status_code, r2.text[:200]])
    r3 = api.get("/files/" + S["snap1_rows"][0]["file_hash"])
    lab.check("/files without token not served", r3.status_code in (401, 404), r3.status_code)


def _norm(value):
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value
    return str(value)


def test_api_results_16_columns(lab):
    """GET /results returns exactly the 16 template columns + hash/ids, equal to SQL rows."""
    api = S["api"]
    items, pages, snap = api.all_results("all")
    S["api_items"] = items  # Retain successful response evidence if a later assertion fails.
    lab.save("evidence/api-results-all.json", items)
    lab.check("results bound to published snapshot", str(snap) == str(S["snap1"]), snap)
    lab.check("item count equals snapshot delivery rows", len(items) == len(S["snap1_rows"]), [len(items), len(S["snap1_rows"])])
    bad_keys = []
    for it in items:
        keys = list(it.keys())
        if keys[:16] != HEADERS16 or not EXTRA_KEYS <= set(keys):
            bad_keys.append(keys)
    lab.check("each item: 16 Chinese keys in template order + markdown_hash/source_id/resource_id/version_id", not bad_keys, bad_keys[:2])
    sql = {(str(r["version_id"]), r["row_number"]): r for r in S["snap1_rows"]}
    mismatch = []
    for it in items:
        r = sql.get((str(it["version_id"]), it.get("row_number")))
        if r is None:
            mismatch.append({"missing_sql": it["version_id"]})
            continue
        pairs = {"资料名称": r["title"], "资料类型": r["document_type"], "文件哈希": r["file_hash"],
                 "文号/案号": r["document_number"], "发布机构": r["issuer"], "状态": r["status"],
                 "来源名称": r["source_name"], "来源详情页": r["detail_url"], "来源记录号": r["source_record_hash"], "markdown_hash": r["markdown_hash"]}
        for k, v in pairs.items():
            if (it[k] or "") != (v or ""):
                mismatch.append({"key": k, "api": it[k], "sql": v})
        order = it["文件顺序"]
        if (r["volume_order"] is None and order not in ("", None)) or (r["volume_order"] is not None and str(order) != str(r["volume_order"])):
            mismatch.append({"key": "文件顺序", "api": order, "sql": r["volume_order"]})
        for k, col in (("发布日期", "published_at"), ("生效日期", "effective_at"), ("失效日期", "expired_at")):
            want = r[col].strftime("%Y-%m-%d") if r[col] else ""
            if it[k] != want:
                mismatch.append({"key": k, "api": it[k], "sql": want})
        want_time = r["acquired_at"].astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if it["采集时间"] != want_time:
            mismatch.append({"key": "采集时间", "api": it["采集时间"], "sql": want_time})
        if it["source_id"] != r["source_id"]:
            mismatch.append({"key": "source_id", "api": it["source_id"], "sql": r["source_id"]})
        # Frozen snapshot naming resolves collisions without mutating Version rows.
        for api_key, sql_key in (("解析后文件", "markdown_file"), ("原始文件", "original_file")):
            base = r[sql_key]
            actual = it[api_key]
            if not base:
                valid = actual in ("", None)
            elif r["volume_order"] is not None:
                valid = actual == base  # Natural volume numbers retain priority.
            else:
                stem, extension = base.rsplit(".", 1)
                valid = re.fullmatch(re.escape(stem) + r"(?:_[0-9]+)?\." + re.escape(extension), actual) is not None
            if not valid:
                mismatch.append({"key": api_key, "api": actual, "base": base, "volume_order": r["volume_order"]})
        if it["原始文件"] and it["解析后文件"].rsplit(".", 1)[0] != it["原始文件"].rsplit(".", 1)[0]:
            mismatch.append({"key": "paired file stems", "markdown": it["解析后文件"], "original": it["原始文件"]})
    lab.check("API business fields equal SQL rows; snapshot names preserve canonical stems/extensions/volume order", not mismatch, mismatch[:10])
    fmt_bad = [it for it in items if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", it["采集时间"])
               or not re.fullmatch(r"[0-9a-f]{64}", it["文件哈希"]) or not re.fullmatch(r"[0-9a-f]{64}", it["markdown_hash"])
               or it["资料类型"] not in {"法律法规", "政策文件", "案例", "行业标准", "通知公告", "财务报告", "会计准则", "税务资料", "其他"}
               or not it["解析后文件"].endswith(".md")]
    lab.check("format: 采集时间 YYYY-MM-DD HH:MM:SS, 64-hex hashes, controlled 资料类型, .md names", not fmt_bad, fmt_bad[:3])
    names = [it["解析后文件"] for it in items]
    lab.check("解析后文件 unique in snapshot", len(names) == len(set(names)), [n for n in names if names.count(n) > 1][:5])
    originals = [it["原始文件"] for it in items if it["原始文件"]]
    lab.check("原始文件 unique in snapshot", len(originals) == len(set(originals)), [n for n in originals if originals.count(n) > 1][:5])
    missing_vals = [it for it in items if "" in (it["资料名称"], it["解析后文件"], it["资料类型"], it["文件哈希"], it["采集时间"])]
    lab.check("required columns non-empty", not missing_vals, missing_vals[:3])
    S["api_items"] = items


def test_api_pagination_and_cursor(lab):
    """limit/cursor pagination is stable, complete, bound to caller/filter."""
    api = S["api"]
    full = S["api_items"]
    paged, pages, _ = api.all_results("all", limit=2)
    key = lambda it: (it["source_id"], it["resource_id"], it.get("row_number"))  # noqa: E731
    lab.check("paged union equals full list in same order", [key(i) for i in paged] == [key(i) for i in full], [len(paged), len(full)])
    lab.check("no duplicates across pages", len({key(i) for i in paged}) == len(paged), len(paged))
    lab.check("multiple pages used", pages >= 2, pages)
    lab.check("stable order by source_id, resource_id, row_number", [key(i) for i in full] == sorted(key(i) for i in full), None)
    first = api.get("/results", "all", {"limit": 2})
    body = first.json()
    lab.check("Link rel=next header present with next_cursor", body.get("next_cursor") and 'rel="next"' in first.headers.get("Link", ""),
              first.headers.get("Link"))
    cur = body["next_cursor"]
    tampered = cur[:-3] + ("AAA" if not cur.endswith("AAA") else "BBB")
    r = api.get("/results", "all", {"limit": 2, "cursor": tampered})
    lab.check("tampered cursor 400 invalid_cursor", r.status_code == 400 and "invalid_cursor" in r.text, [r.status_code, r.text[:200]])
    r = api.get("/results", "limited", {"limit": 2, "cursor": cur})
    lab.check("cursor of another caller rejected 400", r.status_code == 400, [r.status_code, r.text[:200]])
    r = api.get("/results", "all", {"limit": 2, "cursor": cur, "source_id": "dd-033"})
    lab.check("cursor reused with different source filter rejected 400", r.status_code == 400, [r.status_code, r.text[:200]])
    for bad in (0, 101, "x"):
        r = api.get("/results", "all", {"limit": bad})
        lab.check(f"limit={bad} rejected 400", r.status_code == 400, [r.status_code, r.text[:200]])
    r = api.get("/results", "limited", {"source_id": "dd-017"})
    lab.check("unauthorised source_id 403", r.status_code == 403, [r.status_code, r.text[:200]])
    only, _, _ = api.all_results("all", source_id="dd-033")
    lab.check("source_id filter returns only that source", only and all(i["source_id"] == "dd-033" for i in only), len(only))
    expected = [i for i in full if i["source_id"] == "dd-033"]
    lab.check("source filter preserves globally allocated names", only == expected, len(only))
    limited, _, _ = api.all_results("limited")
    lab.check("limited caller sees only authorised source", limited and all(i["source_id"] == "dd-033" for i in limited), {i["source_id"] for i in limited})
    lab.check("caller filtering preserves globally allocated names", limited == expected, len(limited))
    duplicate_expected = [i for i in full if i["source_id"] == "dd-061"]
    duplicate_filter, _, _ = api.all_results("all", source_id="dd-061")
    duplicate_caller, _, _ = api.all_results("single_duplicate")
    lab.check("colliding names remain stable under source/caller filters",
              duplicate_filter == duplicate_caller == duplicate_expected, duplicate_filter)


def test_api_files_bytes(lab):
    """GET /files/{文件哈希|markdown_hash} returns exact bytes with proper headers."""
    api = S["api"]
    problems, comparisons = [], []
    allowed_names = {}
    for item in S["api_items"]:
        allowed_names.setdefault(item["markdown_hash"], set()).add(item["解析后文件"])
        allowed_names.setdefault(item["文件哈希"], set()).add(item["原始文件"] or item["解析后文件"])
    for it in S["api_items"]:
        for kind, digest in (("original", it["文件哈希"]), ("markdown", it["markdown_hash"])):
            r = api.get("/files/" + digest, "all")
            got = sha256(r.content) if r.status_code == 200 else None
            cas = sha256(lab.s3_get(digest))
            comparisons.append({"source_id": it["source_id"], "kind": kind, "sha256": digest, "status": r.status_code, "api_sha256": got,
                                "cas_sha256": cas, "bytes": len(r.content)})
            if r.status_code != 200 or got != digest:
                problems.append(comparisons[-1])
                continue
            h = r.headers
            if h.get("ETag") != f'"{digest}"' or h.get("Content-Length") != str(len(r.content)):
                problems.append({"headers": dict(h), "digest": digest})
            if kind == "markdown" and not h.get("Content-Type", "").startswith("text/markdown"):
                problems.append({"ctype": h.get("Content-Type"), "digest": digest})
            disp = h.get("Content-Disposition", "")
            names = allowed_names[digest]
            if "filename*=" not in disp or not any(quote(name, safe="") in disp for name in names):
                problems.append({"disposition": disp, "allowed_names": sorted(names)})
    lab.save("evidence/files-byte-comparison.json", comparisons)
    lab.check("every 文件哈希 / markdown_hash downloads byte-exact with ETag/Length/Type/Disposition", not problems, problems[:6])
    # spot-check against fixture bytes
    native = S["content"].b["native.pdf"]
    r = api.get("/files/" + sha256(native), "all")
    lab.check("native.pdf downloaded equals served fixture", r.status_code == 200 and r.content == native, r.status_code)


def test_api_files_forbidden(lab):
    """Other-source, internal and non-deliverable hashes return 404."""
    api = S["api"]
    rows = S["snap1_rows"]
    other = next(r for r in rows if r["source_id"] == "dd-017")
    out = {}
    out["other_source_original"] = api.get("/files/" + other["file_hash"], "limited").status_code
    out["other_source_markdown"] = api.get("/files/" + other["markdown_hash"], "limited").status_code
    mine = next(r for r in rows if r["source_id"] == "dd-033")
    out["own_source_original"] = api.get("/files/" + mine["file_hash"], "limited").status_code
    out["inputs_manifest"] = api.get("/files/" + mine["inputs_ref"], "all").status_code
    out["unified_document"] = api.get("/files/" + mine["result_ref"], "all").status_code
    out["listing_page"] = api.get("/files/" + S["listing_hash"], "all").status_code
    noise = lab.q("SELECT body_hash FROM seal.acquisition WHERE request_url=%s AND state='archived'", (S["fx_scan"].base + "/scan/noise.pdf",))
    if noise:
        out["quality_failed_original"] = api.get("/files/" + noise[0]["body_hash"], "all").status_code
    amb = sha256(S["content"].b["amb2.pdf"])
    out["blocked_volume_original"] = api.get("/files/" + amb, "all").status_code
    out["random_hash"] = api.get("/files/" + hashlib.sha256(b"nothing").hexdigest(), "all").status_code
    out["uppercase_hash"] = api.get("/files/" + mine["file_hash"].upper(), "all").status_code
    lab.save("evidence/api-files-forbidden.json", out)
    expected_404 = {k: v for k, v in out.items() if k != "own_source_original" and v != 404}
    lab.check("authorised own-source file served", out["own_source_original"] == 200, out)
    lab.check("unauthorised / internal / non-deliverable hashes all 404", not expected_404, out)


