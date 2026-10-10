"""Shared synthetic Recipe, real CLI paths and independent archived-data oracles."""

import hashlib
import json

import psycopg
from psycopg.rows import dict_row

RECIPE = """import os
import scrapy
from seal.core import SealError
from seal.helpers import attachment_items, attachment_record, diagnostic, html_record, is_attachment_response, json_records, static_resources


class AcceptanceSpider(scrapy.Spider):
    name = "heterogeneous_acceptance"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(seed["url"], callback=self.parse,
                                 errback=self.failed, meta={"seal_role":seed["role"]})

    def failed(self, failure):
        yield {"type":"diagnostic", "code":"download_failed"}

    def parse(self, response):
        mode = self.params.get("mode", "api")
        if mode == "api":
            for item in json_records(response):
                if item.get("type") == "record":
                    if self.params.get("projection") == "short" or os.environ.get("SEAL_E2E_RECORD_NONDET"):
                        item["data"].pop("optional")
                        item["locators"].pop("optional")
                    if self.params.get("bad_type"):
                        item["data"]["active"] = int(item["data"]["active"])
                yield item
        elif mode == "assets":
            if response.meta["seal_role"] == "detail":
                if is_attachment_response(response):
                    yield from self.attachment(response)
                else:
                    yield html_record(response)
            else:
                yield from static_resources(response, iframe_callback=self.iframe,
                                            attachment_callback=self.attachment,
                                            errback=self.failed)
        elif mode == "parse_failure":
            raise SealError("synthetic_parse_failure")

    def iframe(self, response):
        yield html_record(response)

    def attachment(self, response, parent=None):
        try:
            if self.params.get("allow_partial_documents"):
                yield from attachment_items(response, parent=parent)
            else:
                yield attachment_record(response, parent=parent)
        except SealError as exc:
            yield diagnostic(response, exc.code)
"""


def sql(h, query, params=()):
    with psycopg.connect(h.env["SEAL_DATABASE_URL"], row_factory=dict_row) as connection:
        cursor = connection.execute(query, params)
        return cursor.fetchall() if cursor.description is not None else []


def recipe(h):
    root = h.root / "v12-recipe"
    root.mkdir()
    (root / "recipe.py").write_text(RECIPE)
    (root / "recipe.yaml").write_text(
        json.dumps(
            {
                "family": "v12-synthetic-acceptance",
                "entrypoint": "recipe:AcceptanceSpider",
                "params_schema": {"type": "object"},
            }
        )
    )
    return h.cli("recipe", "pack", root)["recipe_version"]


def record_source(h, name, version, paths=None, params=None, **extra):
    h.config(
        name,
        entries=[h.site.url + path for path in (paths or ["/api"])],
        identity="business_key",
        output_schema="record.v1",
        seed_role="api",
        delay=0.0,
        **extra,
    )
    return h.binding(name, recipe=version, params=params)


def run(h, source, binding, *, recheck=False, ok=True, capture=None):
    args = ["run", source, "--binding", binding]
    if recheck:
        args.append("--recheck")
    receipt = h.cli(*args, ok=ok)
    inspected = h.cli("inspect", "run", receipt["run_id"])
    exported = h.cli("export", source, "--run", receipt["run_id"])
    h.check(source + "_receipt_inspect_status", inspected["run"]["status"], receipt["status"])
    h.check(source + "_receipt_export_status", exported["run"]["status"], receipt["status"])
    h.check(source + "_quality_not_evaluated", exported["quality_status"], "not_evaluated")
    if capture:
        h.capture(capture, {"receipt": receipt, "inspect": inspected, "export": exported})
    return receipt, inspected, exported


def by_key(export):
    return {record["record_key"]: record for record in export["records"]}


def pointer(doc, value):
    for token in value[1:].split("/") if value else []:
        token = token.replace("~1", "/").replace("~0", "~")
        doc = doc[int(token)] if isinstance(doc, list) else doc[token]
    return doc


def archived(h, key):
    path = h.root / "archive/objects" / key[:2] / key[2:]
    data = path.read_bytes()
    h.check("archive_digest_" + key, hashlib.sha256(data).hexdigest(), key)
    return data
