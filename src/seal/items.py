"""Validate externally observable document fields against durable inputs."""

import asyncio
import json
from datetime import date
from io import BytesIO

from parsel import Selector
from pydantic import Field
from pypdf import PdfReader

from .config import Strict
from .core import Objects, SealError, digest, public_url, uid
from .db import connect, fenced, j, locked_run, one, record_error


class Candidate(Strict):
    type: str
    url: str
    snapshot_id: str
    observation_id: str
    title: str = Field(min_length=1, max_length=2000)
    body: str = Field(min_length=1, max_length=2000000)
    date: str | None = None
    locators: dict
    attachments: list[dict] = []
    supplementary_inputs: list[dict] = []


def text_value(snapshot, body, locator):
    kind = locator.get("kind")
    if kind == "segments":
        if not locator["segments"]:
            raise SealError("empty_field_segments")
        return "\n".join(text_value(snapshot, body, part) for part in locator["segments"])
    if kind == "xpath":
        selector = Selector(body.decode(snapshot["encoding"] or "utf-8", errors="strict"))
        nodes = selector.xpath(locator["path"])
        if len(nodes) != 1:
            raise SealError("ambiguous_or_missing_field")
        return " ".join(nodes[0].xpath(".//text() | self::text()").getall()).strip()
    if kind == "text":
        value = body.decode(snapshot["encoding"] or "utf-8", errors="strict")
        start, end = locator["start"], locator["end"]
        if not 0 <= start < end <= len(value):
            raise SealError("invalid_text_locator")
        return value[start:end].strip()
    if kind == "json":
        value = json.loads(body)
        for part in locator["pointer"].split("/")[1:]:
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, list) else value[part]
        if not isinstance(value, str):
            raise SealError("json_field_not_string")
        return value.strip()
    if kind == "pdf":
        reader = PdfReader(BytesIO(body), strict=True)
        page = locator["page"]
        if not 1 <= page <= len(reader.pages):
            raise SealError("invalid_pdf_page")
        value = (reader.pages[page - 1].extract_text() or "").strip()
        start, end = locator["start"], locator["end"]
        if not 0 <= start < end <= len(value):
            raise SealError("pdf_text_layer_required")
        return value[start:end].strip()
    raise SealError("unsupported_locator")


def validate_candidate(item):
    value = Candidate.model_validate(item)
    snapshot = Objects().json(value.snapshot_id)
    body = Objects().get(snapshot["body_hash"])
    url = public_url(value.url)
    if (
        snapshot["method"] != "GET"
        or snapshot["status"] != 200
        or public_url(snapshot["url"]) != url
    ):
        raise SealError("main_resource_mismatch")
    if not value.title.strip() or not value.body.strip():
        raise SealError("empty_document")
    if value.date is not None:
        date.fromisoformat(value.date)
    inputs = {value.snapshot_id: (snapshot, body)}
    for entry in value.supplementary_inputs:
        if set(entry) != {"snapshot_id", "observation_id"}:
            raise SealError("invalid_supplementary_input")
        other = Objects().json(entry["snapshot_id"])
        if other["method"] != "GET" or other["status"] != 200:
            raise SealError("invalid_supplementary_response")
        inputs[entry["snapshot_id"]] = (other, Objects().get(other["body_hash"]))
    for field in ("title", "body", "date"):
        expected = getattr(value, field)
        if expected is not None:
            locator = value.locators[field]
            target = inputs.get(locator.get("snapshot_id", value.snapshot_id))
            if target is None or text_value(*target, locator) != expected:
                raise SealError("field_locator_mismatch")
    for attachment in value.attachments:
        if set(attachment) != {"url", "status"} or attachment["status"] != "not_fetched":
            raise SealError("attachment_contract_violation")
        public_url(attachment["url"])
    output = value.model_dump(
        exclude={"type", "snapshot_id", "observation_id", "supplementary_inputs"}
    )
    output["url"] = url
    return output, snapshot


def stage_item(context, item):
    run_id, epoch = context["id"], context["attempt_epoch"]
    if item.get("type") == "diagnostic":
        allowed = {
            "download_failed",
            "http_5xx",
            "http_error",
            "source_rate_limited",
            "unexpected_304",
            "pagination_loop",
            "list_template_changed",
            "missing_document_href",
            "ambiguous_pagination",
            "ambiguous_or_missing_field",
            "pdf_text_layer_required",
            "unsupported_content_type",
        }
        code = item.get("code")
        raise SealError(code if code in allowed else "recipe_diagnostic")
    if item.get("type") == "document_ref":
        url = public_url(item["url"])
        with connect() as c:
            source, run = locked_run(c, run_id)
            fenced(source, run, epoch)
            if item["snapshot_id"] not in {i["snapshot_id"] for i in run["inputs"]}:
                raise SealError("reference_input_mismatch")
            c.execute("UPDATE seal_run SET refs=refs || %s WHERE id=%s", (j([url]), run_id))
        return
    output, snapshot = validate_candidate(item)
    with connect() as c:
        source, run = locked_run(c, run_id)
        fenced(source, run, epoch)
        approved = [
            i
            for i in run["inputs"]
            if i["snapshot_id"] == item["snapshot_id"]
            and i["observation_id"] == item["observation_id"]
        ]
        if not approved:
            raise SealError("candidate_lineage_mismatch")
        observation = one(
            c, "SELECT * FROM seal_fetch_observation WHERE id=%s", (item["observation_id"],)
        )
        if (
            observation["source_id"] != source["id"]
            or observation["snapshot_id"] != item["snapshot_id"]
        ):
            raise SealError("candidate_observation_mismatch")
        if run["mode"] != "replay" and (
            observation["run_id"] != run_id or observation["attempt_epoch"] != epoch
        ):
            raise SealError("stale_observation")
        input_ids = [item["snapshot_id"]]
        for entry in item.get("supplementary_inputs", []):
            if not any(
                i["snapshot_id"] == entry["snapshot_id"]
                and i["observation_id"] == entry["observation_id"]
                for i in run["inputs"]
            ):
                raise SealError("supplementary_lineage_mismatch")
            input_ids.append(entry["snapshot_id"])
        doc_id = uid()
        c.execute(
            "INSERT INTO seal_document(id,source_id,namespace,identity,url) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (doc_id, source["id"], run["namespace"], output["url"], output["url"]),
        )
        doc = one(
            c,
            "SELECT * FROM seal_document WHERE source_id=%s AND namespace=%s AND identity=%s FOR UPDATE",
            (source["id"], run["namespace"], output["url"]),
        )
        previous = (
            one(c, "SELECT * FROM seal_revision WHERE id=%s", (doc["latest_revision"],))
            if doc["latest_revision"]
            else None
        )
        if previous:
            previous_obs = one(
                c, "SELECT * FROM seal_fetch_observation WHERE id=%s", (previous["observation_id"],)
            )
            if (
                previous_obs["run_id"] == run_id
                and previous_obs["attempt_epoch"] == epoch
                and previous["body_hash"] != snapshot["body_hash"]
            ):
                raise SealError("unstable_document_input")
        revision = doc["latest_revision"]
        if not previous or previous["body_hash"] != snapshot["body_hash"]:
            revision = uid()
            # Replay/trial have isolated documents; their observation may be shared.
            observation_key = item["observation_id"]
            row = c.execute(
                "INSERT INTO seal_revision(id,document_id,predecessor,body_hash,snapshot_id,observation_id) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(document_id,observation_id) DO NOTHING RETURNING id",
                (
                    revision,
                    doc["id"],
                    doc["latest_revision"],
                    snapshot["body_hash"],
                    item["snapshot_id"],
                    observation_key,
                ),
            ).fetchone()
            if row is None:
                revision = one(
                    c,
                    "SELECT id FROM seal_revision WHERE document_id=%s AND observation_id=%s",
                    (doc["id"], observation_key),
                )["id"]
        c.execute(
            "UPDATE seal_document SET latest_revision=%s,latest_observation=%s,next_check=now()+(%s * interval '1 second') WHERE id=%s",
            (revision, item["observation_id"], source["config"]["recheck_seconds"], doc["id"]),
        )
        if run["namespace"] == "production":
            c.execute(
                "UPDATE seal_fetch_observation SET eligible=true WHERE id=%s",
                (item["observation_id"],),
            )
        key = digest(
            {
                "namespace": run["namespace"],
                "source": source["id"],
                "identity": doc["identity"],
                "inputs": input_ids,
                "binding": run["binding_id"],
            }
        )
        output_hash = digest(output)
        existing = c.execute("SELECT * FROM seal_result WHERE processing_key=%s", (key,)).fetchone()
        if existing and existing["output_hash"] != output_hash:
            raise SealError("nondeterministic_output")
        result_id = existing["id"] if existing else uid()
        if not existing:
            c.execute(
                "INSERT INTO seal_result(id,document_id,binding_id,processing_key,inputs,output_hash,candidate,checks) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    result_id,
                    doc["id"],
                    run["binding_id"],
                    key,
                    j(input_ids),
                    output_hash,
                    j(output),
                    j({"input_verified": True, "locators_verified": True}),
                ),
            )
        if result_id not in run["result_ids"]:
            c.execute(
                "UPDATE seal_run SET result_ids=result_ids || %s WHERE id=%s",
                (j([result_id]), run_id),
            )


class ItemPipeline:
    @classmethod
    def from_crawler(cls, crawler):
        value = cls()
        value.context = crawler.settings["SEAL_CONTEXT"]
        value.crawler = crawler
        value.io = asyncio.Semaphore(value.context["config"]["concurrency"])
        return value

    async def process_item(self, item):
        from scrapy.exceptions import DropItem

        async with self.io:
            try:
                await asyncio.to_thread(stage_item, self.context, dict(item))
            except Exception as exc:
                code = exc.code if isinstance(exc, SealError) else "invalid_candidate"
                self.crawler.stats.inc_value("seal/errors")
                await asyncio.to_thread(
                    record_error, self.context["id"], self.context["attempt_epoch"], code
                )
                raise DropItem(code) from None
        return item
