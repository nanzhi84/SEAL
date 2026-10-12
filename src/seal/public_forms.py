"""Small closed contract for reviewed public, non-authenticated query forms."""

import re
from urllib.parse import parse_qsl

from .core import SealError

PATTERNS = {
    "page": r"[1-9][0-9]{0,3}",
    "region": r"(?:[0-9]{12})?",
    "query_id": r"[0-9a-f]{32}",
    "empty": r"",
    "management": r"[01]?",
    "date": r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2})?",
}


def check_public_form(config, url, body, content_type):
    if (
        len(body) > 16384
        or content_type.split(b";", 1)[0].strip().lower() != b"application/x-www-form-urlencoded"
    ):
        raise SealError("request_method_rejected")
    rules = [rule for rule in config.get("public_post_rules", []) if rule["url"] == url]
    if len(rules) != 1:
        raise SealError("request_method_rejected")
    try:
        fields = parse_qsl(
            body.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=20,
        )
    except (ValueError, UnicodeError):
        raise SealError("request_method_rejected") from None
    names = [name for name, _ in fields]
    if len(set(names)) != len(names) or set(names) != set(rules[0]["fields"]):
        raise SealError("request_method_rejected")
    for name, value in fields:
        kind = rules[0]["fields"][name]
        if re.fullmatch(PATTERNS[kind], value) is None:
            raise SealError("request_method_rejected")
        if kind == "page" and int(value) > 3000:
            raise SealError("public_form_partition_required")
    return dict(fields)


def representation_body(config, request):
    """Only declared query nonces and ASP.NET hidden state are transport identity.

    The actual request-body digest remains frozen in each Run input. Page, region,
    category and all other values still distinguish independent content resources.
    """
    ignored = {
        name
        for rule in config.get("public_post_rules", [])
        if rule["url"] == request.url
        for name, kind in rule["fields"].items()
        if kind == "query_id"
    }
    if any(rule["url"] == request.url for rule in config.get("public_html_forms", [])):
        ignored.update({"__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"})
    return [
        (name, value)
        for name, value in parse_qsl(request.body.decode("ascii"), keep_blank_values=True)
        if name not in ignored
    ]
