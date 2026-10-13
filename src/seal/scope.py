"""Shared URL boundary for SDK traversal, HTTP redirects and browser subrequests."""

import posixpath
from urllib.parse import unquote, urlsplit

from .core import SealError, check_address, public_url


def valid_prefix(path):
    return (
        isinstance(path, str)
        and path.startswith("/")
        and ".." not in path
        and unquote(path) == path
        and "?" not in path
        and "#" not in path
        and "\\" not in path
    )


def paths_for(config, host, path):
    if host not in config["allowed_hosts"]:
        return False
    scopes = config.get("host_path_scopes", {})
    prefixes = scopes.get(host, []) if scopes else config["allowed_path_prefixes"]
    path = posixpath.normpath(unquote(path))
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def guard(config, url, *, policy=False):
    url = public_url(url)
    parsed = urlsplit(url)
    check_address(parsed.hostname)
    allowed_policy = policy and parsed.path == "/robots.txt" and not parsed.query
    if parsed.hostname not in config["allowed_hosts"] or not (
        allowed_policy or paths_for(config, parsed.hostname, parsed.path)
    ):
        raise SealError("request_out_of_scope")
    return url
