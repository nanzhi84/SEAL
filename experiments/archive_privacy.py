"""Privacy checks for experimental publication; never used by the Runtime archive."""

import re
from html.parser import HTMLParser

BODY_KEY = (
    rb"(?:(?:(?:access|refresh|id|csrf|xsrf|auth)[_-]?)?token|api[_-]?key|"
    rb"(?:user[_-]?)?password|passwd|pwd|(?:proxy[_-]?)?authorization|"
    rb"(?:client[_-]?)?secret|secret[_-]?key|(?:set[_-]?)?cookie|"
    rb"session(?:[_-]?id|[_-]?token)?|jsessionid|credential(?:s)?)"
)
BODY_SECRET = re.compile(
    rb"(?:(?<![\w-])[\"']?(?P<plain>" + BODY_KEY + rb")[\"']?|"
    rb"\[\s*[\"'](?P<bracket>" + BODY_KEY + rb")[\"']\s*\])\s*[:=]\s*",
    re.I,
)
BODY_VALUE = re.compile(rb'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s,;<>{}\[\]"\']+')
BODY_PLACEHOLDER_END = re.compile(rb"\s*(?:[,;}\])><]|$)")


class _CredentialInputs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.populated = False
        self.text_controls = []

    def mark_value(self, sensitive, value):
        if sensitive and (value or "").strip():
            self.populated = True

    def handle_starttag(self, tag, attrs):
        if tag == "option" and self.text_controls:
            for _, sensitive in self.text_controls:
                for key, value in attrs:
                    if key == "value":
                        self.mark_value(sensitive, value)
        if tag not in {"input", "textarea", "select"}:
            return
        sensitive = any(
            key == "type"
            and (value or "").lower() == "password"
            or key in ("name", "id")
            and re.fullmatch(BODY_KEY, (value or "").encode("utf-8"), re.I)
            for key, value in attrs
        )
        # Duplicate attributes are invalid HTML, but no populated credential
        # may disappear merely because another attribute carries an empty value.
        for key, value in attrs:
            if key == "value":
                self.mark_value(sensitive, value)
        if tag in {"textarea", "select"}:
            self.text_controls.append((tag, sensitive))

    def handle_data(self, value):
        for _, sensitive in self.text_controls:
            self.mark_value(sensitive, value)

    def handle_endtag(self, tag):
        self.text_controls = [control for control in self.text_controls if control[0] != tag]


def contains_sensitive_body(body):
    """Reject actual scalar credentials without rejecting empty public forms.

    Quoted nonempty values (including the string \"null\") are credentials; only
    complete unquoted null/undefined and empty strings are safe placeholders.
    Unknown JavaScript expressions remain rejected: an empty HTML input cannot
    prove what script setters, wrapper functions or later reads will produce.
    This checks experimental publication eligibility and never changes archived bytes.
    """
    parser = _CredentialInputs()
    if any(tag in body.lower() for tag in (b"<input", b"<textarea", b"<select")):
        parser.feed(body.decode("latin1"))
        if parser.populated:
            return True
    for match in BODY_SECRET.finditer(body):
        value = BODY_VALUE.match(body, match.end())
        if value is None:
            if body[match.end() : match.end() + 1] in {b'"', b"'", b"{", b"["}:
                return True  # Truncated quotes/containers cannot prove an empty scalar.
            continue
        raw = value[0]
        placeholder = raw.lower() in {b"null", b"undefined"} or (
            raw[:1] in {b'"', b"'"} and not raw[1:-1].strip()
        )
        if placeholder:
            if BODY_PLACEHOLDER_END.match(body, value.end()):
                continue
        return True
    return False
