"""CLI/HTTP/PostgreSQL acceptance of the public document representation contract."""

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import psycopg
from fixtures import install

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/v1.2-acceptance"))
SPEC = importlib.util.spec_from_file_location(
    "seal_v12_acceptance", ROOT / "experiments/v1.2-acceptance/acceptance.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
Harness = MODULE.Harness

from runtime_support import sql  # noqa: E402

FIELD_SPEC = importlib.util.spec_from_file_location(
    "seal_field_support", ROOT / "experiments/field-contracts/support.py"
)
FIELDS = importlib.util.module_from_spec(FIELD_SPEC)
FIELD_SPEC.loader.exec_module(FIELDS)

RECIPE = """import scrapy
from seal.helpers import follow, html_record

class DocumentSpider(scrapy.Spider):
    name = "document_ir_acceptance"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context

    async def start(self):
        for seed in self.context["seeds"]:
            if self.params.get("post_pages"):
                for page in self.params["post_pages"]:
                    yield scrapy.FormRequest(seed["url"], formdata={"page": str(page)},
                                             callback=self.parse, meta={"seal_role": seed["role"]})
                continue
            meta = {"seal_role": seed["role"]}
            if self.params.get("selector"):
                meta["seal_document_content_css"] = self.params["selector"]
            yield scrapy.Request(seed["url"], callback=self.parse, meta=meta)

    def parse(self, response):
        if response.url.endswith(("/index", "/base")):
            for link in response.css("a[href]"):
                yield follow(response, link.attrib["href"], "attachment", self.parse)
        elif self.params.get("extract") and response.url.endswith("/article"):
            yield html_record(response)
"""


def exercise(h):
    install(h)
    recipe_dir = h.root / "recipe"
    recipe_dir.mkdir()
    (recipe_dir / "recipe.py").write_text(RECIPE)
    (recipe_dir / "recipe.yaml").write_text(
        json.dumps(
            {
                "family": "document-ir-acceptance",
                "entrypoint": "recipe:DocumentSpider",
                "params_schema": {"type": "object"},
            }
        )
    )
    version = h.cli("recipe", "pack", recipe_dir)["recipe_version"]
    h.config(
        "ir",
        entries=[h.site.url + "/ir/index"],
        output_schema="record.v1",
        document_ir=True,
        delay=0.0,
        concurrency=1,
    )
    binding = h.binding("ir", version, {"extract": False})
    run = h.cli("collect", "ir", "--binding", binding, ok=False)
    exported = h.cli("export", "ir", "--run", run["run_id"])
    h.capture("collection", {"receipt": run, "export": exported})
    docs = {Path(row["url"]).name: row for row in exported["content_documents"]}
    h.check("document_only_has_no_business_records", exported["records"], [])
    h.check("list_saved_despite_bad_child", "index" in docs)
    h.check("csv_is_table", [c["type"] for c in docs["data.csv"]["children"]], ["table"])
    h.check(
        "json_array_keeps_null", docs["data.json"]["children"][0]["value"], [1, None, {"ok": True}]
    )
    h.check(
        "article_links_saved", any(c["type"] == "document_ref" for c in docs["article"]["children"])
    )
    h.check("image_is_image", docs["picture.png"]["children"][0]["type"], "image")
    h.check("corrupt_image_has_no_success", "bad.png" not in docs)
    h.check("mislabeled_image_has_no_success", "bad.webp" not in docs)
    h.check(
        "html_has_table_and_image",
        {c["type"] for c in docs["article"]["children"]},
        {"article", "table", "image", "document_ref"},
    )
    h.check("bad_pdf_no_success", "bad.pdf" not in docs)
    h.check(
        "source_format_retained",
        (docs["picture.png"]["source_format"], docs["picture.png"]["mime_type"]),
        ("image", "image/png"),
    )
    pending = next(ref for ref in docs["index"]["refs"] if ref["url"].endswith("/ir/bad.pdf"))
    h.check(
        "failed_target_has_fk_but_is_pending",
        bool(pending["document_id"]) and not pending["resolved"],
    )
    shared = [
        ref for row in docs.values() for ref in row["refs"] if ref["url"].endswith("/ir/data.csv")
    ]
    h.check("shared_target_uses_one_identity", len({ref["document_id"] for ref in shared}), 1)
    h.check("shared_target_resolves", all(ref["resolved"] for ref in shared))
    h.check(
        "bad_pdf_archive_present",
        bool(
            sql(
                h,
                "SELECT body_hash FROM seal_fetch_observation WHERE url LIKE %s AND body_hash IS NOT NULL",
                ("%/ir/bad.pdf",),
            )
        ),
    )
    h.check(
        "snapshot_and_parser_version",
        bool(docs["data.csv"]["snapshot_id"] and docs["data.csv"]["parser_version"]),
    )
    h.capture("document", h.cli("inspect", "content-document", docs["article"]["document_id"]))
    discovery_events = h.cli("inspect", "run", run["run_id"])["discovery"]["events"]
    h.check(
        "parse_failure_attributed_to_snapshot",
        any(
            event["reason"] == "image_parse_failed" and event["snapshot_id"]
            for event in discovery_events
        ),
    )

    h.config(
        "ir_base",
        entries=[h.site.url + "/ir/base"],
        output_schema="record.v1",
        document_ir=True,
        delay=0.0,
        concurrency=1,
    )
    base_binding = h.binding("ir_base", version, {})
    base_run = h.cli("collect", "ir_base", "--binding", base_binding)
    base_export = h.cli("export", "ir_base", "--run", base_run["run_id"])
    base_page = next(
        row for row in base_export["content_documents"] if row["url"].endswith("/base")
    )
    h.check(
        "base_href_ref_matches_download",
        base_page["refs"][0]["url"],
        h.site.url + "/ir/assets/data.csv",
    )
    h.check("base_href_ref_resolved", base_page["refs"][0]["resolved"])
    h.check(
        "base_href_image_resolved",
        next(c["url"] for c in base_page["children"] if c["type"] == "image"),
        h.site.url + "/ir/assets/picture.png",
    )
    h.capture("base", {"receipt": base_run, "export": base_export})

    h.config(
        "ir_pages",
        entries=[h.site.url + "/ir/error", h.site.url + "/ir/empty"],
        output_schema="record.v1",
        document_ir=True,
        delay=0.0,
        concurrency=1,
    )
    pages_binding = h.binding("ir_pages", version, {})
    pages_run = h.cli("collect", "ir_pages", "--binding", pages_binding, ok=False)
    pages_export = h.cli("export", "ir_pages", "--run", pages_run["run_id"])
    h.check("error_page_rejected", "fallback_error_page" in pages_run["errors"])
    h.check(
        "empty_html_is_valid_document",
        [(Path(row["url"]).name, row["children"]) for row in pages_export["content_documents"]],
        [("empty", [])],
    )
    h.capture("pages", {"receipt": pages_run, "export": pages_export})
    try:
        sql(
            h,
            "INSERT INTO seal_content_ref(version_id,ordinal,target_document_id,url) VALUES(%s,%s,%s,%s)",
            (
                docs["article"]["document_version_id"],
                999,
                "missing-target",
                h.site.url + "/ir/missing",
            ),
        )
        rejected = False
    except psycopg.errors.ForeignKeyViolation:
        rejected = True
    h.check("relation_fk_rejects_missing_target", rejected)

    h.config(
        "ir_sitemap",
        entries=[h.site.url + "/ir/sitemap.xml"],
        output_schema="record.v1",
        document_ir=True,
        seed_role="sitemap",
        delay=0.0,
        concurrency=1,
    )
    sitemap_binding = h.binding("ir_sitemap", version, {})
    sitemap_run = h.cli("collect", "ir_sitemap", "--binding", sitemap_binding)
    sitemap_export = h.cli("export", "ir_sitemap", "--run", sitemap_run["run_id"])
    h.check("sitemap_is_not_content_document", sitemap_export["content_documents"], [])
    h.capture("sitemap", {"receipt": sitemap_run, "export": sitemap_export})

    h.config(
        "ir_nav",
        entries=[h.site.url + "/ir/nav-list"],
        output_schema="record.v1",
        document_ir=True,
        delay=0.0,
        concurrency=1,
    )
    nav_binding = h.binding("ir_nav", version, {})
    nav_run = h.cli("collect", "ir_nav", "--binding", nav_binding)
    nav_export = h.cli("export", "ir_nav", "--run", nav_run["run_id"])
    h.check("navigation_only_list_is_document", len(nav_export["content_documents"]), 1)
    h.check(
        "navigation_only_list_keeps_link",
        nav_export["content_documents"][0]["refs"][0]["url"],
        h.site.url + "/ir/note.txt",
    )
    h.capture("nav-list", {"receipt": nav_run, "export": nav_export})

    invalid = h.config(
        "ir_invalid",
        entries=[h.site.url + "/ir/note.txt"],
        output_schema="record.v1",
        document_ir=False,
    )
    invalid["document_ir"] = "yes"
    invalid_path = h.root / "invalid-document-config.json"
    invalid_path.write_text(json.dumps(invalid))
    invalid_receipt = h.cli("source", "apply", invalid_path, ok=False)
    h.check("invalid_opt_in_rejected", invalid_receipt["category"], "ValidationError")

    h.config(
        "ir_formats",
        entries=[
            h.site.url + "/ir/" + suffix
            for suffix in ("pdf", "docx", "xls", "xlsx", "null", "scalar", "object")
        ],
        output_schema="record.v1",
        document_ir=True,
        seed_role="attachment",
        delay=0.0,
        concurrency=1,
    )
    formats_binding = h.binding("ir_formats", version, {"extract": False})
    formats_run = h.cli("collect", "ir_formats", "--binding", formats_binding)
    formats_export = h.cli("export", "ir_formats", "--run", formats_run["run_id"])
    formats = {Path(row["url"]).name: row for row in formats_export["content_documents"]}
    h.check("pdf_is_article", formats["pdf"]["children"][0]["type"], "article")
    h.check(
        "docx_has_single_article",
        [c["type"] for c in formats["docx"]["children"]].count("article"),
        1,
    )
    h.check("xls_is_table", all(c["type"] == "table" for c in formats["xls"]["children"]))
    h.check(
        "xlsx_two_named_sheets",
        [c["name"] for c in formats["xlsx"]["children"]],
        ["First", "Second"],
    )
    h.check("json_null", formats["null"]["children"][0]["value"], None)
    h.check("json_scalar", formats["scalar"]["children"][0]["value"], 42)
    h.check("json_object", formats["object"]["children"][0]["value"], {"ok": True, "count": 1})
    h.capture("formats", {"receipt": formats_run, "export": formats_export})
    repeat = h.cli("collect", "ir_formats", "--binding", formats_binding)
    repeated = h.cli("export", "ir_formats", "--run", repeat["run_id"])
    repeated_by_url = {row["url"]: row for row in repeated["content_documents"]}
    h.check(
        "repeat_collect_reuses_versions",
        {row["url"]: row["document_version_id"] for row in formats_export["content_documents"]},
        {url: row["document_version_id"] for url, row in repeated_by_url.items()},
    )
    h.capture("repeat", {"receipt": repeat, "export": repeated})

    updated_binding = h.binding("ir_formats", version, {"extract": False, "parser_rule": "v2"})
    updated = h.cli("collect", "ir_formats", "--binding", updated_binding)
    updated_export = h.cli("export", "ir_formats", "--run", updated["run_id"])
    updated_by_url = {row["url"]: row for row in updated_export["content_documents"]}
    h.check(
        "new_binding_creates_distinct_versions",
        all(
            row["document_version_id"] != updated_by_url[row["url"]]["document_version_id"]
            for row in formats_export["content_documents"]
        ),
    )
    h.check(
        "rule_change_keeps_raw_snapshot",
        all(
            row["snapshot_id"] == updated_by_url[row["url"]]["snapshot_id"]
            for row in formats_export["content_documents"]
        ),
    )
    h.capture("rule-change", {"receipt": updated, "export": updated_export})

    h.config(
        "ir_post",
        entries=[h.site.url + "/ir/post"],
        output_schema="record.v1",
        document_ir=True,
        seed_role="api",
        methods=["GET", "HEAD", "POST"],
        public_post_rules=[{"url": h.site.url + "/ir/post", "fields": {"page": "page"}}],
        delay=0.0,
        concurrency=1,
    )
    post_binding = h.binding("ir_post", version, {"post_pages": [1, 2]})
    post_run = h.cli("collect", "ir_post", "--binding", post_binding)
    post_export = h.cli("export", "ir_post", "--run", post_run["run_id"])
    h.check(
        "post_variants_have_distinct_document_identities",
        len({row["document_id"] for row in post_export["content_documents"]}),
        2,
    )
    h.check(
        "post_body_variants_kept",
        sorted(row["children"][0]["value"]["page"] for row in post_export["content_documents"]),
        [1, 2],
    )
    h.capture("post", {"receipt": post_run, "export": post_export})
    post_network_count = len(h.site.ledger)
    post_replay = h.cli("replay", post_run["run_id"])
    h.check("post_replay_zero_network", len(h.site.ledger), post_network_count)
    h.check("post_replay_complete", post_replay["status"], "complete")
    h.capture(
        "post-replay",
        {
            "receipt": post_replay,
            "export": h.cli("export", "ir_post", "--run", post_replay["run_id"]),
        },
    )

    # A separate source proves business extraction only occurs after its document exists.
    h.config(
        "ir_business",
        entries=[h.site.url + "/ir/article"],
        output_schema="record.v1",
        document_ir=True,
        delay=0.0,
        concurrency=1,
    )
    business_binding = h.binding("ir_business", version, {"extract": True})
    business = h.cli("collect", "ir_business", "--binding", business_binding)
    business_export = h.cli("export", "ir_business", "--run", business["run_id"])
    h.check(
        "business_record_has_document_input",
        bool(business_export["records"][0]["document_version_ids"]),
    )
    h.capture("business", {"receipt": business, "export": business_export})

    mapping = {
        "Cơ quan ban hành:": "issuing_authority",
        "Năm ban hành:": "issued_year",
        "Lĩnh vực:": "subject_areas",
    }
    mapped_version = FIELDS.pack(h, FIELDS.manifest(mapping=mapping), "mapped-recipe")
    h.config(
        "ir_mapped",
        entries=[h.site.url + "/ir/fields"],
        output_schema="record.v1",
        identity="business_key",
        seed_role="api",
        document_ir=True,
        delay=0.0,
        concurrency=1,
    )
    mapped_binding = h.binding("ir_mapped", mapped_version, {})
    mapped_run = h.cli("collect", "ir_mapped", "--binding", mapped_binding)
    mapped_export = h.cli("export", "ir_mapped", "--run", mapped_run["run_id"])
    mapped = mapped_export["records"][0]
    h.check(
        "field_mapping_after_document",
        mapped["data"],
        {
            "issuing_authority": "Public agency",
            "issued_year": 2026,
            "subject_areas": None,
            "local": "kept",
        },
    )
    h.check("mapped_null_has_no_locator", "subject_areas" not in mapped["field_evidence"])
    h.check(
        "mapped_record_links_document_version",
        mapped["document_version_ids"],
        [mapped_export["content_documents"][0]["document_version_id"]],
    )
    h.capture("mapped", {"receipt": mapped_run, "export": mapped_export})
    sql(
        h,
        "UPDATE seal_record SET next_check=now() - interval '1 second' WHERE source_id=%s",
        ("ir_mapped",),
    )
    recheck = h.cli("collect", "ir_mapped", "--binding", mapped_binding, "--recheck")
    rechecked = h.cli("export", "ir_mapped", "--run", recheck["run_id"])
    h.check("recheck_keeps_document_link", bool(rechecked["records"][0]["document_version_ids"]))
    h.capture("recheck", {"receipt": recheck, "export": rechecked})
    h.config(
        "ir_badselect",
        entries=[h.site.url + "/ir/article"],
        output_schema="record.v1",
        document_ir=True,
        delay=0.0,
        concurrency=1,
    )
    bad_binding = h.binding("ir_badselect", version, {"extract": True, "selector": "#missing"})
    bad = h.cli("collect", "ir_badselect", "--binding", bad_binding, ok=False)
    bad_export = h.cli("export", "ir_badselect", "--run", bad["run_id"])
    h.check("invalid_selector_is_explicit", "document_content_selector_invalid" in bad["errors"])
    h.check("business_is_blocked_on_document_failure", bad_export["records"], [])
    h.check(
        "invalid_selector_keeps_archive",
        bool(
            sql(
                h,
                "SELECT body_hash FROM seal_fetch_observation WHERE run_id=%s AND body_hash IS NOT NULL",
                (bad["run_id"],),
            )
        ),
    )
    h.capture("bad-selector", {"receipt": bad, "export": bad_export})
    before = len(h.site.ledger)
    replay = h.cli("replay", business["run_id"])
    h.check("replay_zero_network", len(h.site.ledger), before)
    h.check("replay_success", replay["status"], "complete")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    h = Harness(args.output)
    error = None
    try:
        h.start()
        exercise(h)
    except Exception as exc:
        error = exc
    finally:
        h.save("document-ir", error)
        manifest = args.output / "manifest.json"
        data = json.loads(manifest.read_text())
        data.update(
            command="uv run --frozen python experiments/document-ir/acceptance.py --output <new-directory>",
            prerequisites="PostgreSQL 17 tools, Python 3.12, uv; isolated local cluster and loopback HTTP",
            git_head=__import__("subprocess")
            .check_output(["git", "rev-parse", "HEAD"], text=True)
            .strip(),
            input_sha256={
                key: hashlib.sha256(value[1]).hexdigest()
                for key, value in getattr(h, "fixtures", {}).items()
            },
        )
        data["code"].update(
            {
                str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(Path("experiments/document-ir").glob("*.py"))
            }
        )
        manifest.write_text(json.dumps(data, indent=2))
        public = Path("artifacts/ci-public")
        public.mkdir(parents=True, exist_ok=True)
        (public / "documents.json").write_text(
            json.dumps(
                {
                    "status": "FAIL" if error else "PASS",
                    "git_head": data["git_head"],
                    "assertions": len(h.assertions),
                    "passed": sum(item["status"] == "PASS" for item in h.assertions),
                    "scope": data["scope"],
                },
                indent=2,
            )
        )
        h.close()
    if error:
        raise error
    return 0


if __name__ == "__main__":
    sys.exit(main())
