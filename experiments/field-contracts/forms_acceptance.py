"""Before implementation: closed archived-parent HTML form through CLI/HTTP/PG."""

import json
from urllib.parse import parse_qs

FORM_RECIPE = """import scrapy
from seal.helpers import record_item

class PublicFormSpider(scrapy.Spider):
    name = "public_html_form_acceptance"
    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
    async def start(self):
        yield scrapy.Request(self.context["seeds"][0]["url"], callback=self.form, meta={"seal_role":"list"})
    def form(self, response):
        attack = self.params.get("attack")
        data = {"category":"6", "year":"2026"}
        if attack == "hidden": data["state"] = "tampered"
        if attack == "option": data["category"] = "999"
        if attack == "extra": data["unexpected"] = "value"
        if attack == "size": data["state"] = "x" * 140000
        meta = {"seal_role":"api", "seal_parent_url":response.url, "seal_parent_snapshot_id":response.meta["seal_snapshot_id"], "seal_parent_observation_id":response.meta["seal_observation_id"]}
        if attack == "parent":
            meta.pop("seal_parent_snapshot_id")
            meta["_seal_explicit_policy_parent"] = True
        request = scrapy.FormRequest.from_response(response, formxpath="//form[@id='form1']", formdata=data, clickdata={"name":"search"}, callback=self.parse, meta=meta)
        if attack == "cookie": request.headers["Cookie"] = "session=forbidden"
        if attack == "action": request = request.replace(url=response.urljoin("/unapproved"))
        yield request
    def parse(self, response):
        data = response.json()["data"]
        yield record_item(response, record_type="htmlform",record_key=response.url, detail_url=response.url, data=data,locators={k:{"kind":"json", "pointer":"/data/"+k} for k in data},key_locator={"kind":"response_url"})
"""


def exercise_forms(h):
    handler = h.site.server.RequestHandlerClass
    original_get = handler.do_GET
    original_post = getattr(handler, "do_POST", None)
    state = "public-render-state-" + "x" * 20000
    form = (
        '<form id="form1" method="post" action="/form-result"><input type="hidden" name="state" value="'
        + state
        + '"><input name="keyword" type="text" value=""><select name="category"><option value="0">All</option><option value="6">Policy</option></select><select name="year"><option value="2026">2026</option></select><input name="search" type="submit" value="Find"></form>'
    )

    def reply(request, body, content_type):
        request.send_response(200)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def get(request):
        if request.path != "/public-form":
            return original_get(request)
        h.site.ledger.append({"method": "GET", "path": request.path, "status": 200})
        reply(request, form.encode(), "text/html; charset=utf-8")

    def post(request):
        if request.path != "/form-result":
            if original_post:
                return original_post(request)
            request.send_error(405)
            return
        body = request.rfile.read(int(request.headers["Content-Length"]))
        fields = parse_qs(body.decode(), keep_blank_values=True)
        h.site.ledger.append(
            {
                "method": "POST",
                "path": request.path,
                "status": 200,
                "body_bytes": len(body),
                "exact_parent_state": fields.get("state") == [state],
                "category": fields.get("category"),
            }
        )
        reply(
            request,
            json.dumps({"data": {"agency": "Government", "year": 2026}}).encode(),
            "application/json",
        )

    handler.do_GET, handler.do_POST = get, post
    try:
        root = h.root / "form-recipe"
        root.mkdir()
        (root / "recipe.py").write_text(FORM_RECIPE)
        (root / "recipe.yaml").write_text(
            json.dumps(
                {
                    "family": "public-html-form",
                    "entrypoint": "recipe:PublicFormSpider",
                    "params_schema": {"type": "object"},
                }
            )
        )
        version = h.cli("recipe", "pack", root)["recipe_version"]
        rule = {
            "url": h.site.url + "/form-result",
            "parent_url": h.site.url + "/public-form",
            "form_xpath": '//form[@id="form1"]',
            "fields": ["state", "keyword", "category", "year", "search"],
            "select_fields": ["category", "year"],
            "submit": {"name": "search", "value": "Find"},
            "max_body_bytes": 131072,
        }
        h.config(
            "htmlform",
            entries=[h.site.url + "/public-form"],
            methods=["GET", "POST"],
            public_html_forms=[rule],
            output_schema="record.v1",
            identity="business_key",
            delay=0.0,
        )
        binding = h.binding("htmlform", version)
        before = len(h.site.ledger)
        run = h.cli("collect", "htmlform", "--binding", binding)
        exported = h.cli("export", "htmlform", "--run", run["run_id"])
        h.capture("htmlform", {"receipt": run, "export": exported})
        h.check(
            "htmlform_user_result",
            exported["records"][0]["data"],
            {"agency": "Government", "year": 2026},
        )
        sent = [row for row in h.site.ledger[before:] if row["method"] == "POST"]
        h.check("htmlform_one_post", len(sent), 1)
        h.check("htmlform_large_state_preserved", sent[0]["exact_parent_state"])
        h.check("htmlform_approved_body_over_old_limit", sent[0]["body_bytes"] > 16384)
        before = len(h.site.ledger)
        replay = h.cli("replay", run["run_id"])
        repeated = h.cli("export", "htmlform", "--run", replay["run_id"])
        h.check("htmlform_zero_network_replay", len(h.site.ledger), before)
        h.check(
            "htmlform_replay_data", repeated["records"][0]["data"], exported["records"][0]["data"]
        )
        h.capture("htmlform-replay", {"receipt": replay, "export": repeated})
        for attack in (
            "hidden",
            "option",
            "extra",
            "size",
            "parent",
            "cookie",
            "action",
            "no_opt_in",
        ):
            name = "form_" + attack
            extra = {} if attack == "no_opt_in" else {"public_html_forms": [rule]}
            h.config(
                name,
                entries=[h.site.url + "/public-form"],
                methods=["GET", "POST"],
                output_schema="record.v1",
                identity="business_key",
                delay=0.0,
                **extra,
            )
            binding = h.binding(name, version, {"attack": attack})
            before = len(h.site.ledger)
            rejected = h.cli("collect", name, "--binding", binding, ok=False)
            exported = h.cli("export", name, "--run", rejected["run_id"])
            h.check(
                attack + "_no_post",
                any(row["method"] == "POST" for row in h.site.ledger[before:]),
                False,
            )
            h.check(attack + "_no_record", exported["records"], [])
            h.check(attack + "_reported_failure", bool(exported["run"]["errors"]))
            h.capture(name, {"receipt": rejected, "export": exported})
    finally:
        handler.do_GET = original_get
        if original_post:
            handler.do_POST = original_post
        else:
            del handler.do_POST
