"""Typed Record input validation and Crawl-scoped strict JSON evidence reuse."""

import json
import math
import re
import sys
from collections import OrderedDict
from threading import RLock
from typing import Literal

from pydantic import Field

from .attachments import located_attachment, parse_attachment
from .config import Strict
from .core import Objects, SealError, canonical, public_url


class ParentRequest(Strict):
    url: str
    role: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    method: Literal["GET"] = "GET"


class RecordCandidate(Strict):
    type: Literal["record"]
    record_type: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    record_key: str = Field(min_length=1, max_length=2048)
    schema_version: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    data: dict = Field(min_length=1, max_length=1000)
    snapshot_id: str
    observation_id: str
    locators: dict
    key_locator: dict
    detail_url: str | None = None
    frozen_parent_request: ParentRequest | None = None
    supplementary_inputs: list[dict] = Field(default_factory=list, max_length=1000)


def json_value(value, depth=0):
    """Reject Python-only values, nonfinite numbers and unbounded nesting."""
    if depth > 32:
        raise SealError("record_data_too_deep")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise SealError("record_nonfinite_number")
        return
    if type(value) is list:
        for child in value:
            json_value(child, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for child in value.values():
            json_value(child, depth + 1)
        return
    raise SealError("record_data_not_json")


def typed_equal(left, right):
    """JSON types are significant: false != 0 and 1 != 1.0."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(
            typed_equal(left[key], right[key]) for key in left
        )
    if type(left) is list:
        return len(left) == len(right) and all(
            typed_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _invalid_constant(_value):
    raise SealError("record_nonfinite_number")


def _unique_json_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise SealError("ambiguous_json_object_key")
        value[key] = child
    return value


def _decode(body):
    return json.loads(body, parse_constant=_invalid_constant, object_pairs_hook=_unique_json_object)


def _retained_bytes(value):
    # Account for the actual decoded tree, including Python container overhead.
    # IDs prevent charging shared singleton/interned values more than once.
    pending, seen, size = [value], set(), 0
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        size += sys.getsizeof(current)
        if type(current) is dict:
            pending.extend(current.keys())
            pending.extend(current.values())
        elif type(current) is list:
            pending.extend(current)
    return size


class JsonInputCache:
    """Private to one ItemPipeline; only hash-verified immutable inputs enter it.

    Keep at most 16 decoded trees / 64 MiB, evict least recently used responses,
    and release everything at pipeline close. A lock prevents duplicate parses
    when Scrapy validates concurrent Records from the same API response. Large
    responses remain valid but are not retained beyond their individual lookup.
    This cache never replaces Objects.get: every Record still verifies bytes.
    """

    def __init__(self, max_entries=16, max_bytes=64 * 1024 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.entries = OrderedDict()
        self.bytes = self.parses = self.hits = self.evictions = 0
        self.peak_entries = self.peak_bytes = 0
        self.lock = RLock()

    def load(self, body_hash, body):
        with self.lock:
            if body_hash in self.entries:
                self.hits += 1
                self.entries.move_to_end(body_hash)
                return self.entries[body_hash][0]
            self.parses += 1
            value = _decode(body)
            cost = _retained_bytes(value)
            if cost > self.max_bytes:
                return value
            while self.entries and (
                len(self.entries) >= self.max_entries or self.bytes + cost > self.max_bytes
            ):
                _, (_, previous_cost) = self.entries.popitem(last=False)
                self.bytes -= previous_cost
                self.evictions += 1
            self.entries[body_hash] = (value, cost)
            self.bytes += cost
            self.peak_entries = max(self.peak_entries, len(self.entries))
            self.peak_bytes = max(self.peak_bytes, self.bytes)
            return value

    def close(self):
        with self.lock:
            self.entries.clear()
            self.bytes = 0

    def stats(self):
        with self.lock:
            return {
                "parses": self.parses,
                "hits": self.hits,
                "evictions": self.evictions,
                "retained_entries": len(self.entries),
                "retained_bytes": self.bytes,
                "peak_entries": self.peak_entries,
                "peak_bytes": self.peak_bytes,
            }


class _RecordJsonInput:
    """Keep one decoded input for this Record, including an oversized response.

    Field checks are grouped by body hash, so a tree that exceeds the Crawl LRU
    budget is decoded once and released before validating the next input. The
    key is checked in its input's group as well. Nothing outlives this validation.
    """

    def __init__(self, cache):
        self.cache = cache
        self.body_hash = self.value = None
        self.attachment_key = self.attachment_value = None

    def load(self, body_hash, body):
        if self.body_hash != body_hash:
            self.value = self.cache.load(body_hash, body) if self.cache else _decode(body)
            self.body_hash = body_hash
        return self.value

    def attachment(self, kind, body_hash, body):
        key = kind, body_hash
        if self.attachment_key != key:
            self.attachment_value = parse_attachment(kind, body)
            self.attachment_key = key
        return self.attachment_value


def json_pointer(body, pointer, *, cache=None, body_hash=None):
    """Resolve RFC 6901 without accepting ambiguous array indices or escapes."""
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise SealError("invalid_json_pointer")
    # Standalone callers have no verified archive identity. Their bytes must
    # never enter the shared cache under an absent or implicit identity.
    value = cache.load(body_hash, body) if cache is not None and body_hash else _decode(body)
    if not pointer:
        return value
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise SealError("invalid_json_pointer")
        token = token.replace("~1", "/").replace("~0", "~")
        try:
            if type(value) is list:
                if not re.fullmatch(r"0|[1-9][0-9]*", token):
                    raise SealError("invalid_json_pointer")
                value = value[int(token)]
            elif type(value) is dict:
                value = value[token]
            else:
                raise SealError("invalid_json_pointer")
        except (IndexError, KeyError, ValueError):
            raise SealError("missing_json_pointer") from None
    return value


def located_value(snapshot, body, locator, cache=None):
    # The old text locator keeps its historical string/strip semantics.
    from .items import text_value

    if locator.get("kind") in {"html_blocks", "html_links", "html_date", "response_url"}:
        from .fallback import html_locator_value

        return html_locator_value(snapshot, body, locator)
    if locator.get("kind") == "json" and "transform" not in locator:
        return json_pointer(
            body, locator.get("pointer"), cache=cache, body_hash=snapshot["body_hash"]
        )
    if locator.get("kind") in ("xls", "docx"):
        structure = (
            cache.attachment(locator["kind"], snapshot["body_hash"], body)
            if isinstance(cache, _RecordJsonInput)
            else None
        )
        return located_attachment(body, locator, structure=structure)
    return text_value(snapshot, body, locator)


def _located_key(value, target, cache):
    key_locator = value.key_locator
    if key_locator.get("kind") == "response_url":
        if set(key_locator) - {"kind", "snapshot_id"}:
            raise SealError("invalid_record_key_locator")
        key = public_url(target[0]["url"])
        if value.detail_url is None or public_url(value.detail_url) != key:
            raise SealError("record_key_locator_mismatch")
    elif key_locator.get("transform") == "key_string":
        if key_locator.get("kind") != "json":
            raise SealError("invalid_record_key_transform")
        key = json_pointer(
            target[1], key_locator.get("pointer"), cache=cache, body_hash=target[0]["body_hash"]
        )
        if type(key) not in (str, int):
            raise SealError("invalid_record_key_transform")
        key = str(key)
    else:
        key = located_value(*target, key_locator, cache)
    if type(key) is not str or key != value.record_key:
        raise SealError("record_key_locator_mismatch")


def validate_record(item, cache=None):
    value = RecordCandidate.model_validate(item)
    json_value(value.data)
    if len(canonical(value.data)) > 2000000:
        raise SealError("record_data_too_large")
    if set(value.locators) != set(value.data):
        raise SealError("record_locator_fields_mismatch")
    objects = Objects()
    snapshot = objects.json(value.snapshot_id)
    if snapshot["method"] != "GET" or snapshot["status"] != 200:
        raise SealError("invalid_record_response")
    inputs = {value.snapshot_id: (snapshot, objects.get(snapshot["body_hash"]))}
    lineage = [{"snapshot_id": value.snapshot_id, "observation_id": value.observation_id}]
    for entry in value.supplementary_inputs:
        if set(entry) != {"snapshot_id", "observation_id"}:
            raise SealError("invalid_supplementary_input")
        other = objects.json(entry["snapshot_id"])
        if other["method"] != "GET" or other["status"] != 200:
            raise SealError("invalid_supplementary_response")
        inputs[entry["snapshot_id"]] = (other, objects.get(other["body_hash"]))
        if entry not in lineage:
            lineage.append(entry)
    groups = {}
    for field, expected in value.data.items():
        locator = value.locators[field]
        if not isinstance(locator, dict):
            raise SealError("invalid_record_locator")
        target = inputs.get(locator.get("snapshot_id", value.snapshot_id))
        if target is None:
            raise SealError("record_field_locator_mismatch")
        groups.setdefault(target[0]["body_hash"], []).append((target, locator, expected))
    key_locator = value.key_locator
    key_target = inputs.get(key_locator.get("snapshot_id", value.snapshot_id))
    if key_target is None:
        raise SealError("record_key_locator_mismatch")
    for body_hash, checks in groups.items():
        local = _RecordJsonInput(cache)
        for target, locator, expected in checks:
            if not typed_equal(located_value(*target, locator, local), expected):
                raise SealError("record_field_locator_mismatch")
        if key_target[0]["body_hash"] == body_hash:
            _located_key(value, key_target, local)
    if key_target[0]["body_hash"] not in groups:
        _located_key(value, key_target, _RecordJsonInput(cache))
    output = value.model_dump(
        exclude={"type", "snapshot_id", "observation_id", "supplementary_inputs"}
    )
    # Locators without an explicit snapshot refer to this immutable main input.
    output["primary_snapshot_id"] = value.snapshot_id
    if value.detail_url is not None:
        output["detail_url"] = public_url(value.detail_url)
    if value.frozen_parent_request is not None:
        output["frozen_parent_request"]["url"] = public_url(value.frozen_parent_request.url)
    elif value.detail_url is None:
        raise SealError("record_parent_request_required")
    return output, lineage
