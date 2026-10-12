"""Reviewed business channels; Runtime owns network, policy, persistence and Replay."""

import scrapy
from amac import Amac
from education import Education
from permit import Permit
from publications import Publications
from safe import Safe

from seal.core import SealError
from seal.helpers import diagnostic, follow


class BusinessSpider(scrapy.Spider):
    name = "public_business_resource"
    seeded_collection = True
    discovery_defaults = {"max_depth": 100, "max_query_variants": 1000, "exclude_patterns": []}

    def __init__(self, params, context, **kwargs):
        super().__init__(**kwargs)
        self.params, self.context = params, context
        self.expansion_enabled = (
            context["mode"] != "recheck" and context.get("recheck_plan") is None
        )
        self.allowed_domains = context["config"]["allowed_hosts"]
        handlers = {
            "dd-017": Publications,
            "dd-041": Publications,
            "dd-250": Amac,
            "dd-247": Amac,
            "dd-468": Education,
            "dd-102": Safe,
            "dd-357": Permit,
        }
        if params["source"] not in handlers:
            raise SealError("business_contract_not_ready")
        self.handler = handlers[params["source"]](self)

    async def start(self):
        for seed in self.context["seeds"]:
            yield scrapy.Request(
                seed["url"],
                method=seed.get("method", "GET"),
                body=seed.get("body", "").encode("ascii"),
                headers={"Content-Type": "application/x-www-form-urlencoded"}
                if seed.get("method") == "POST"
                else {},
                callback=self.parse,
                errback=self.failed,
                meta={"seal_role": seed["role"]},
            )

    def next(self, response, url, role, *, parent=None):
        request = follow(
            response,
            url,
            role,
            self.parse,
            self.failed,
            cb_kwargs={"parent": parent} if parent else None,
        )
        if self.params["source"] == "dd-357":
            request.headers["Referer"] = response.url
        request.meta["seal_depth"] = response.meta.get("seal_depth", 0) + 1
        return request

    def failed(self, failure):
        response = getattr(failure.value, "response", None)
        if response is not None:
            yield diagnostic(response, "http_5xx" if response.status >= 500 else "http_error")
        # Runtime retains guard and transport reasons; never overwrite them.

    def parse(self, response, parent=None):
        try:
            yield from self.handler.parse(response, parent)
        except (SealError, ValueError, KeyError, TypeError) as exc:
            yield diagnostic(
                response, exc.code if isinstance(exc, SealError) else "business_template_changed"
            )
