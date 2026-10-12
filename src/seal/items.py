"""Adapters may emit bounded diagnostics; Runtime is the sole content producer."""

import asyncio
import re

from scrapy.exceptions import DropItem

from .db import record_error
from .discovery import mark_inputs_failed


class ItemPipeline:
    @classmethod
    def from_crawler(cls, crawler):
        value = cls()
        value.context = crawler.settings["SEAL_CONTEXT"]
        value.crawler = crawler
        return value

    async def process_item(self, item):
        code = (
            item.get("code")
            if item.get("type") == "diagnostic"
            else "adapter_content_item_rejected"
        )
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
            code = "recipe_diagnostic"
        self.crawler.stats.inc_value("seal/errors")
        await asyncio.to_thread(
            record_error, self.context["id"], self.context["attempt_epoch"], code
        )
        if item.get("snapshot_id"):
            await asyncio.to_thread(mark_inputs_failed, self.context, [item["snapshot_id"]], code)
        raise DropItem(code)
