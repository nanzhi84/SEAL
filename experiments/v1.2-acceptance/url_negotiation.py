"""E2E: negotiated representations stay distinct; unstable same inputs fail."""

import json

RECIPE = """import scrapy
from seal.helpers import json_records

class Spider(scrapy.Spider):
    name = "negotiation"

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context

    async def start(self):
        mode = self.params.get("mode", "stable")
        for seed in self.context["seeds"]:
            variants = [(a, l, "api", "") for a in ["application/json", "text/plain"]
                        for l in ["en", "zh"]]
            if mode != "stable":
                variants = [("application/json", "en", "api", ""),
                            ("application/json", "en", "api", "")]
            if mode == "roles":
                variants[1] = ("application/json", "en", "detail", "")
            if mode == "redirect":
                variants = [("application/json", "en", "api", "/r1"),
                            ("application/json", "en", "api", "/r2")]
            if mode == "interleaved":
                variants.insert(1, ("application/json", "zh", "api", ""))
            for accept, language, role, suffix in variants:
                yield scrapy.Request(seed["url"] + suffix, callback=self.parse,
                    headers={"Accept": accept, "Accept-Language": language},
                    dont_filter=mode != "stable", meta={"seal_role": role})

    def parse(self, response):
        yield from json_records(response)
"""


def exercise_negotiation(h, check):
    handler = h.site.server.RequestHandlerClass
    original = handler.do_GET
    observed = []
    mode = "stable"

    def get(request):
        if request.path.startswith("/negotiation/r"):
            request.send_response(302)
            request.send_header("Location", "/negotiation")
            request.send_header("Content-Length", "0")
            request.end_headers()
            h.site.ledger.append({"method": "GET", "path": request.path, "status": 302})
            return
        if request.path != "/negotiation":
            return original(request)
        accept = request.headers.get("Accept")
        language = request.headers.get("Accept-Language")
        observed.append((accept, language))
        h.site.ledger.append({"method": "GET", "path": request.path, "status": 200})
        key = accept + ":" + language + (str(len(observed)) if mode != "stable" else "")
        body = json.dumps(
            {
                "data": {
                    "data": {"dataList": [{"id": key, "name": "Public representation " + language}]}
                }
            }
        ).encode()
        request.send_response(200)
        request.send_header("Content-Type", accept)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    handler.do_GET = get
    try:
        root = h.root / "negotiation-recipe"
        root.mkdir()
        (root / "recipe.py").write_text(RECIPE)
        (root / "recipe.yaml").write_text(
            json.dumps(
                {
                    "family": "url-negotiation-regression",
                    "entrypoint": "recipe:Spider",
                    "params_schema": {"type": "object"},
                }
            )
        )
        version = h.cli("recipe", "pack", root)["recipe_version"]
        for mode in ["same", "roles", "redirect", "interleaved", "stable"]:
            name = "negotiation_" + mode
            observed.clear()
            h.config(
                name,
                entries=[h.site.url + "/negotiation"],
                identity="business_key",
                output_schema="record.v1",
                seed_role="api",
                delay=0.0,
                concurrency=1,
            )
            binding = h.binding(name, recipe=version, params={"mode": mode})
            try:
                receipt = h.cli("run", name, "--binding", binding, ok=mode == "stable")
            except AssertionError:
                receipt = h.receipts[-1]["result"]
            export = h.cli("export", name, "--run", receipt["run_id"])
            if mode != "stable":
                check(name + "_partial", receipt["status"], "partial")
                check(
                    name + "_same_representation_body_change_detected",
                    "unstable_resource_input" in receipt["errors"],
                    True,
                )
                h.capture(
                    name, {"receipt": receipt, "export": export, "observed_headers": observed}
                )
                continue
            expected = [
                (accept, language)
                for accept in ["application/json", "text/plain"]
                for language in ["en", "zh"]
            ]
            check("negotiation_collect_complete", receipt["status"], "complete")
            check("negotiation_four_http_representations", sorted(observed), sorted(expected))
            check(
                "negotiation_four_records",
                sorted(r["record_key"] for r in export["records"]),
                sorted(accept + ":" + language for accept, language in expected),
            )
            offset = len(observed)
            replay = h.cli("replay", receipt["run_id"])
            replay_export = h.cli("export", name, "--run", replay["run_id"])
            check("negotiation_replay_complete", replay["status"], "complete")
            check("negotiation_replay_four_records", len(replay_export["records"]), 4)
            check("negotiation_replay_zero_http", len(observed), offset)
            h.capture(
                name,
                {
                    "receipt": receipt,
                    "export": export,
                    "replay": replay,
                    "replay_export": replay_export,
                    "observed_headers": observed,
                },
            )
    finally:
        handler.do_GET = original
