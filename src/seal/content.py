"""Strict bounded JSON values for document nodes, independent of business schemas."""

import math

from .core import SealError


def json_value(value, depth=0):
    if depth > 100:
        raise SealError("invalid_document_json")
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list):
        for child in value:
            json_value(child, depth + 1)
        return value
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for child in value.values():
            json_value(child, depth + 1)
        return value
    raise SealError("invalid_document_json")
