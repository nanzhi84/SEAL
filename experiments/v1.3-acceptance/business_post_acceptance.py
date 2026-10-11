"""Before implementation: approved public form POST through CLI, archive and Replay.

Unknown fields, duplicate fields, wrong endpoint, secret-like fields and oversized
body must fail before network. Body-aware frozen parents must survive Recheck;
same URL/different page bodies are distinct inputs and Replay makes no GET/POST.
"""

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments/v1.2-acceptance"))
from acceptance import Harness  # noqa: E402

RECIPE = """import scrapy
from seal.helpers import record_item, xpath_locator, node_text
class FormSpider(scrapy.Spider):
    name = 'public_form_contract'
    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params,self.context=params,context
    async def start(self):
        for seed in self.context['seeds']:
            if seed.get('method') == 'POST':
                yield scrapy.Request(seed['url'],method='POST',body=seed['body'].encode(),headers={'Content-Type':'application/x-www-form-urlencoded'},callback=self.parse,meta={'seal_role':'api'})
            else:
                yield scrapy.Request(seed['url'],callback=self.entry,meta={'seal_role':'list'})
    def entry(self,response):
        bodies = ['page=1', 'page=2'] if self.params['body'] == 'page=1' and self.params['path'] == '/public/query' else [self.params['body']]
        for body in bodies:
            yield scrapy.Request(response.urljoin(self.params['path']),method='POST',body=body.encode(),headers={'Content-Type':'application/x-www-form-urlencoded'},callback=self.parse,meta={'seal_role':'api'})
    def parse(self,response):
        node=response.css('p')[0]
        locator=xpath_locator(node)
        yield record_item(response,record_type='public_form_row',record_key=node_text(node),data={'key':node_text(node)},locators={'key':locator},key_locator=locator)
"""


def exercise(h):
    handler = h.site.server.RequestHandlerClass
    original = getattr(handler, "do_POST", None)

    def post(request):
        raw = request.rfile.read(int(request.headers.get("Content-Length", "0")))
        h.site.ledger.append({"method": "POST", "path": request.path, "size": len(raw)})
        body = (
            b"<html><p>public-row-2</p></html>"
            if raw == b"page=2"
            else b"<html><p>public-row-1</p></html>"
        )
        request.send_response(200)
        request.send_header("Content-Type", "text/html")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    handler.do_POST = post
    try:
        package = h.root / "form-recipe"
        package.mkdir()
        (package / "recipe.py").write_text(RECIPE)
        (package / "recipe.yaml").write_text(
            json.dumps(
                {
                    "family": "public-form-contract",
                    "entrypoint": "recipe:FormSpider",
                    "params_schema": {"type": "object"},
                }
            )
        )
        version = h.cli("recipe", "pack", package)["recipe_version"]
        cases = [
            ("allowed", "/public/query", "page=1", True),
            ("field", "/public/query", "password=test", False),
            ("duplicate", "/public/query", "page=1&page=2", False),
            ("endpoint", "/other/query", "page=1", False),
            ("body", "/public/query", "page=" + "1" * 70000, False),
        ]
        for name, path, body, allowed in cases:
            source = "public_post_" + name
            h.config(
                source,
                entries=[h.site.url + "/html/list"],
                output_schema="record.v1",
                methods=["GET", "HEAD", "POST"],
                public_post_rules=[
                    {"url": h.site.url + "/public/query", "fields": {"page": "page"}}
                ],
            )
            binding = h.binding(source, version, {"path": path, "body": body})
            before = len(h.site.ledger)
            run = h.cli("collect", source, "--binding", binding, ok=allowed)
            posts = [x for x in h.site.ledger[before:] if x.get("method") == "POST"]
            h.check(name + "_post_count", len(posts), 2 * int(allowed))
            exported = h.cli("export", source, "--run", run["run_id"])
            h.check(name + "_records", len(exported["records"]), 2 * int(allowed))
            h.capture(name, {"run": run, "export": exported})
            if allowed:
                h.select(source, binding, 0)
                recheck = h.cli("run", source, "--recheck")
                h.check("post_recheck_complete", recheck["status"], "complete")
                h.check(
                    "post_recheck_two_frozen_bodies",
                    recheck["report"]["scope_evidence"]["recheck_plan"]["unique_request_count"],
                    2,
                )
                count = len(h.site.ledger)
                replay = h.cli("replay", run["run_id"])
                h.check("post_replay_complete", replay["status"], "complete")
                h.check("post_replay_zero_network", len(h.site.ledger), count)
                h.capture("recheck", recheck)
                h.capture("replay", replay)
    finally:
        if original is None:
            del handler.do_POST
        else:
            handler.do_POST = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    h, error = Harness(args.output), None
    try:
        h.start()
        exercise(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save("business-public-post", error)
        manifest_path = args.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "v1.3"
        manifest["command"] = (
            "uv run --frozen python experiments/v1.3-acceptance/business_post_acceptance.py --output <new-directory>"
        )
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        h.close()
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
