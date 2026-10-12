"""Closed public HTML forms reconstructed from this attempt's archived parent."""

from urllib.parse import parse_qsl

from lxml import etree
from scrapy import FormRequest
from scrapy.http import HtmlResponse

from .core import Objects, SealError, public_url
from .db import connect


def _fields(body):
    try:
        pairs = parse_qsl(
            body.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=100,
        )
    except (ValueError, UnicodeError):
        raise SealError("public_html_form_body_invalid") from None
    result = dict(pairs)
    if len(result) != len(pairs):
        raise SealError("public_html_form_duplicate_field")
    return result


def _parent(context, request, rule):
    identity = request.meta.get("seal_parent_snapshot_id")
    observation = request.meta.get("seal_parent_observation_id")
    if not identity or not observation:
        raise SealError("public_html_form_parent_required")
    with connect() as connection:
        accepted = connection.execute(
            """SELECT o.id FROM seal.fetch_observation o JOIN seal.run r ON r.id=o.run_id
               WHERE o.id=%s AND o.run_id=%s AND o.attempt_epoch=%s AND o.source_id=%s
                 AND o.snapshot_id=%s AND o.status=200 AND r.attempt_epoch=%s
                 AND r.status='running' AND EXISTS (
                   SELECT 1 FROM jsonb_array_elements(r.inputs) entry
                   WHERE entry->>'snapshot_id'=%s AND entry->>'observation_id'=o.id)""",
            (
                observation,
                context["id"],
                context["attempt_epoch"],
                context["source_id"],
                identity,
                context["attempt_epoch"],
                identity,
            ),
        ).fetchone()
    if accepted is None:
        raise SealError("public_html_form_parent_mismatch")
    objects = Objects()
    snapshot = objects.json(identity)
    if (
        snapshot["status"] != 200
        or snapshot["method"] not in {"GET", "POST"}
        or snapshot["response_type"] != "HtmlResponse"
        or public_url(snapshot["url"]) != rule["parent_url"]
    ):
        raise SealError("public_html_form_parent_mismatch")
    return HtmlResponse(
        url=snapshot["url"],
        body=objects.get(snapshot["body_hash"]),
        encoding=snapshot["encoding"],
    )


def check_html_form(config, context, request):
    """Validate one opt-in POST without enlarging the existing small POST contract."""
    rules = [rule for rule in config["public_html_forms"] if rule["url"] == request.url]
    if len(rules) != 1:
        raise SealError("request_method_rejected")
    rule = rules[0]
    content_type = request.headers.get("Content-Type", b"")
    if (
        len(request.body) > rule["max_body_bytes"]
        or content_type.split(b";", 1)[0].strip().lower() != b"application/x-www-form-urlencoded"
    ):
        raise SealError("public_html_form_body_invalid")
    sent = _fields(request.body)
    if set(sent) != set(rule["fields"]):
        raise SealError("public_html_form_fields_mismatch")
    response = _parent(context, request, rule)
    try:
        forms = response.selector.root.xpath(rule["form_xpath"])
    except etree.XPathError:
        raise SealError("public_html_form_xpath_invalid") from None
    if not isinstance(forms, list) or len(forms) != 1 or getattr(forms[0], "tag", None) != "form":
        raise SealError("public_html_form_not_unique")
    form = forms[0]
    controls = form.xpath(".//input|.//select|.//textarea|.//button")
    if form.get("method", "get").upper() != "POST" or any(
        node.tag == "input" and node.get("type", "").lower() in {"password", "file"}
        for node in controls
    ):
        raise SealError("public_html_form_controls_rejected")
    submit = rule["submit"]
    buttons = [
        node
        for node in controls
        if node.tag == "input"
        and node.get("type", "").lower() == "submit"
        and node.get("name") == submit["name"]
        and node.get("value", "") == submit["value"]
        and "disabled" not in node.attrib
    ]
    if len(buttons) != 1:
        raise SealError("public_html_form_submit_mismatch")
    selected = {}
    for name in rule["select_fields"]:
        matches = [node for node in controls if node.tag == "select" and node.get("name") == name]
        if len(matches) != 1 or "multiple" in matches[0].attrib or "disabled" in matches[0].attrib:
            raise SealError("public_html_form_select_invalid")
        options = matches[0].xpath(
            "./option[@value and not(@disabled)]"
            "|./optgroup[not(@disabled)]/option[@value and not(@disabled)]"
        )
        if sent[name] not in {node.get("value") for node in options}:
            raise SealError("public_html_form_option_rejected")
        selected[name] = sent[name]
    try:
        expected = FormRequest.from_response(
            response,
            formxpath=rule["form_xpath"],
            formdata=selected,
            clickdata=submit,
        )
    except (ValueError, TypeError):
        raise SealError("public_html_form_invalid") from None
    if public_url(expected.url) != rule["url"] or expected.method != "POST":
        raise SealError("public_html_form_action_mismatch")
    if _fields(expected.body) != sent:
        raise SealError("public_html_form_defaults_mismatch")
