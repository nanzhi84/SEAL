"""A frozen recipe drives public CLI paths; assertions live outside the recipe."""

import json

RECIPE = """import scrapy
from seal.core import SealError
from seal.documents import inspect_document_version
from seal.helpers import diagnostic, document_item, link_requests, record_item, save_document


def year_text(value, params):
    return str(value)


class DocumentsAcceptanceSpider(scrapy.Spider):
    name = "unified_documents_acceptance"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(seed["url"], callback=self.parse, errback=self.failed,
                                 meta={"seal_role": seed["role"]})

    def failed(self, failure):
        yield {"type": "diagnostic", "code": "download_failed"}

    def parse(self, response):
        # Discovery is independent of parsing, including an access-denied page.
        if b"html" in response.headers.get("Content-Type", b""):
            yield from link_requests(response, ".documents a[href]", "detail", self.parse, self.failed)
            yield from link_requests(response, "a.next[href]", "list", self.parse, self.failed)
            yield from link_requests(response, "a.attachment[href]:not([href$='not-fetched.txt'])",
                                     "attachment", self.parse, self.failed)
        options = self.params.get("options", {})
        try:
            if self.params.get("extract_business"):
                saved = save_document(response, self.context, options=options)
                document = inspect_document_version(saved["document_version_id"])
                value = document["children"][0]["value"]
                record = record_item(response, record_type="document_business",
                    record_key=response.url, detail_url=response.url,
                    schema_version="record.v1",
                    data={"year": value["year"], "authority": value["authority"]},
                    locators={"year": {"kind": "document",
                        "version_id": saved["document_version_id"],
                        "pointer": "/children/0/value/year"}},
                    key_locator={"kind": "response_url"}, document_versions=[saved])
                record["data_schema_id"] = self.context["field_contract"]["data_schema"]["$id"]
                if self.params.get("defect") == "nested_locator":
                    other = save_document(response, self.context, options={"format": "json"})
                    record["data"]["year"] = [value["year"]]
                    record["locators"]["year"] = {"kind": "values", "locators": [{
                        "kind": "document", "version_id": other["document_version_id"],
                        "snapshot_id": saved["snapshot_id"],
                        "pointer": "/children/0/value/year"}]}
                yield record
            else:
                item = document_item(response, options=options)
                defect = self.params.get("defect")
                if defect == "node":
                    item["content"]["children"].append({"type": "group", "children": []})
                elif defect == "foreign_key":
                    item["content"]["children"].append({"type": "document_ref",
                        "url": response.urljoin("/documents/plain.txt"),
                        "relation": "attachment", "document_id": "invented-document-id"})
                elif defect == "input":
                    item["observation_id"] = "invented-observation-id"
                yield item
        except SealError as exc:
            yield diagnostic(response, exc.code)
"""


def manifest(*, business=False):
    value = {
        "family": "document-pipeline-acceptance",
        "entrypoint": "recipe:DocumentsAcceptanceSpider",
        "params_schema": {"type": "object"},
    }
    if business:
        value.update(
            extraction_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "urn:seal:document-business:v1",
                "type": "object",
                "properties": {
                    "year": {"type": ["integer", "null"]},
                    "authority": {"type": ["string", "null"]},
                },
                "required": ["year", "authority"],
                "additionalProperties": False,
            },
            field_mapping={"year": "issued_year", "authority": "issuing_authority"},
            field_transforms={
                "year": {
                    "entrypoint": "recipe:year_text",
                    "params": {},
                    "params_schema": {"type": "object", "additionalProperties": False},
                }
            },
            data_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "urn:seal:document-business:normalized:v1",
                "type": "object",
                "properties": {
                    "issued_year": {"type": ["string", "null"]},
                    "issuing_authority": {"type": ["string", "null"]},
                },
                "required": ["issued_year", "issuing_authority"],
                "additionalProperties": False,
            },
        )
    return value


def pack(h, name="documents-recipe", *, business=False, nested=False):
    path = h.root / name
    path.mkdir()
    (path / "recipe.py").write_text(RECIPE)
    config = manifest(business=business)
    if nested:
        config["extraction_schema"]["properties"]["year"] = {
            "type": ["array", "null"],
            "items": {"type": "integer"},
        }
    (path / "recipe.yaml").write_text(json.dumps(config))
    return h.cli("recipe", "pack", path)["recipe_version"]


def source(h, name, version, paths, params=None, **extra):
    h.config(
        name,
        entries=[h.site.url + path for path in paths],
        output_schema="document.v1",
        delay=0.0,
        concurrency=1,
        **extra,
    )
    return h.binding(name, version, params)


def collect(h, name, binding, *, ok=True, recheck=False, label=None):
    command = ["collect", name, "--binding", binding]
    if recheck:
        command.append("--recheck")
    run = h.cli(*command, ok=ok)
    exported = h.cli("export", name, "--documents", "--run", run["run_id"])
    inspected = h.cli("inspect", "run", run["run_id"])
    h.capture(label or name, {"receipt": run, "export": exported, "inspect": inspected})
    return run, exported, inspected


def by_url(exported):
    return {row["url"]: row for row in exported["documents"]}
