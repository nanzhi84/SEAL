"""Shared boundary primitives; never include untrusted values in error messages."""

import hashlib
import ipaddress
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4


class SealError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def uid():
    return str(uuid4())


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


SENSITIVE = re.compile(
    r"token|password|secret|authorization|api[-_]?key|session|signature|credential", re.I
)
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
    This runs before content-addressed storage and never changes archived bytes.
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


def safe_url(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SealError("invalid_url")
        host = parts.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        port = parts.port
        if port and (parts.scheme, port) not in (("http", 80), ("https", 443)):
            host += ":" + str(port)
        # Preserve non-sensitive query order; sorting could change source semantics.
        query = urlencode(
            [
                (k, "REDACTED" if SENSITIVE.search(k) else v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parts.scheme, host, parts.path or "/", query, ""))
    except ValueError:
        raise SealError("invalid_url") from None


def public_url(url):
    parts = urlsplit(url)
    if (
        parts.username
        or parts.password
        or any(SENSITIVE.search(k) for k, _ in parse_qsl(parts.query))
    ):
        raise SealError("sensitive_url_rejected")
    return safe_url(url)


def check_address(host):
    """Basic address boundary; explicit loopback opt-in is for isolated acceptance."""
    if host.rstrip(".").lower() == "localhost" or host.lower().endswith(
        (".localhost", ".local", ".internal")
    ):
        raise SealError("non_public_address")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return  # Hostname scope is enforced separately; this is not a DNS-rebinding sandbox.
    if not address.is_global and not (
        address.is_loopback and os.environ.get("SEAL_ALLOW_LOOPBACK") == "1"
    ):
        raise SealError("non_public_address")


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uid() + ".tmp")
    try:
        with temp.open("xb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temp.unlink(missing_ok=True)


class Objects:
    def __init__(self):
        self.root = Path(os.environ.get("SEAL_ARCHIVE", ".seal")).resolve()

    def path(self, key):
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise SealError("invalid_object_key")
        return self.root / "objects" / key[:2] / key[2:]

    def put(self, body):
        key = digest(body)
        path = self.path(key)
        if path.exists():
            self.get(key)
        else:
            atomic_write(path, body)
        return key

    def get(self, key):
        try:
            body = self.path(key).read_bytes()
        except OSError:
            raise SealError("archive_unreadable") from None
        if digest(body) != key:
            raise SealError("archive_corrupt")
        return body

    def put_json(self, value):
        return self.put(canonical(value))

    def json(self, key):
        return json.loads(self.get(key))
