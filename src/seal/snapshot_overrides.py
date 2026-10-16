"""Bounded operator configuration; UUIDs and duplicate identities fail closed."""

import json
from pathlib import Path
from uuid import UUID

from .core import SealError

MAX_BYTES = 2 * 1024 * 1024
MAX_OVERRIDES = 10000


def uuid(value):
    if not isinstance(value, str):
        raise SealError("invalid_version_overrides")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise SealError("invalid_version_overrides") from None
    return value


def normalize(overrides):
    if not isinstance(overrides, list) or len(overrides) > MAX_OVERRIDES:
        raise SealError("invalid_version_overrides")
    result = {}
    for item in overrides:
        if not isinstance(item, dict) or set(item) != {"resource_id", "version_id"}:
            raise SealError("invalid_version_overrides")
        resource_id, version_id = uuid(item["resource_id"]), uuid(item["version_id"])
        if resource_id in result:
            raise SealError("duplicate_override_resource")
        result[resource_id] = version_id
    return [{"resource_id": r, "version_id": v} for r, v in sorted(result.items())]


def unique_pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise SealError("duplicate_override_key")
        data[key] = value
    return data


def load(path):
    try:
        with Path(path).open("rb") as file:
            data = file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise SealError("version_overrides_too_large")
        value = json.loads(data, object_pairs_hook=unique_pairs)
        if not isinstance(value, dict) or set(value) != {"overrides"}:
            raise SealError("invalid_version_overrides")
        return normalize(value["overrides"])
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise SealError("invalid_version_overrides") from None
