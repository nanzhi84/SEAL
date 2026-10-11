"""CLI fixtures specified before field-contract implementation."""

import copy
import json

DIALECT = "https://json-schema.org/draft/2020-12/schema"


def schema(identity, properties):
    return {
        "$schema": DIALECT,
        "$id": "urn:seal:schema:" + identity,
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


RECIPE = '''import scrapy
from seal.helpers import record_item
from seal.core import SealError
from datetime import datetime


def year(value, *, format):
    return datetime.strptime(value, format).year


def integer(value):
    return int(value)


def prefix(value, *, prefix):
    return prefix + value


class FieldsSpider(scrapy.Spider):
    name = "fields_acceptance"
    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(seed["url"], callback=self.parse, meta={"seal_role":"api"})
    def parse(self, response):
        data = response.json()["data"]
        locators = {key:{"kind":"json", "pointer":"/data/"+key.replace("~","~0").replace("/","~1")} for key, value in data.items() if value is not None}
        for key, rule in self.context.get("field_contract", {}).get("field_parsers", {}).items():
            if data.get(key) is not None:
                try:
                    data[key] = globals()[rule["entrypoint"].split(":")[1]](data[key], **rule.get("params", {}))
                except (ValueError, TypeError, OverflowError):
                    data[key] = None
                    locators.pop(key, None)
                else:
                    locators[key]["parser"] = rule
        if self.params.get("missing_key"):
            data.pop(self.params["missing_key"])
            locators.pop(self.params["missing_key"], None)
        if self.params.get("missing_locator"):
            locators.pop(self.params["missing_locator"], None)
        if self.params.get("bad_type"):
            data[self.params["bad_type"]] = 17
        item = record_item(response, record_type="synthetic", record_key="one", data=data, locators=locators, key_locator={"kind":"json","pointer":"/id"}, detail_url=response.url)
        if "field_contract" in self.context:
            item["data_schema_id"] = self.params.get("schema_id", self.context["field_contract"]["data_schema"]["$id"])
        yield item
'''


def manifest(identity="a", mapping=None, transforms=None, parsers=None):
    properties = {
        "Cơ quan ban hành:": {"type": ["string", "null"]},
        "Năm ban hành:": {"type": ["integer", "null"]},
        "Lĩnh vực:": {"type": ["array", "null"], "items": {"type": "string"}},
        "local": {"type": ["string", "null"]},
    }
    source = schema(identity + ":v1", properties)
    value = {
        "family": "fields-acceptance",
        "entrypoint": "recipe:FieldsSpider",
        "params_schema": {"type": "object"},
        "extraction_schema": source,
    }
    if mapping is not None:
        value["field_mapping"] = mapping
    if transforms:
        value["field_transforms"] = transforms
        value["data_schema"] = schema(
            identity + ":internal:v1",
            {mapping.get(key, key) if mapping else key: copy.deepcopy(kind) for key, kind in properties.items()},
        )
    if parsers:
        value["field_parsers"] = parsers
    return value


def pack(h, manifest, name="bundle"):
    root = h.root / name
    root.mkdir(exist_ok=True)
    (root / "recipe.py").write_text(RECIPE)
    (root / "recipe.yaml").write_text(json.dumps(manifest, ensure_ascii=False))
    return h.cli("recipe", "pack", root)["recipe_version"]


def source(h, name, version, path="a", params=None):
    h.config(name, entries=[h.site.url + "/fields/" + path], identity="business_key", output_schema="record.v1", seed_role="api", delay=0.0)
    return h.binding(name, version, params)


def collect(h, name, binding, ok=True):
    run = h.cli("collect", name, "--binding", binding, ok=ok)
    exported = h.cli("export", name, "--run", run["run_id"])
    h.capture(name, {"receipt": run, "inspect": h.cli("inspect", "run", run["run_id"]), "export": exported})
    return run, exported
