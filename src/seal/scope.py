"""One host/path boundary shared by configuration and the downloader guard."""

import posixpath
from urllib.parse import unquote, urlsplit


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
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
        for prefix in prefixes
    )


def robots_policy_request(config, request):
    """A narrowly identified policy fetch is allowed outside business paths.

    This does not exempt policy HTTP from addresses, redirects, method, headers,
    budget or deadlines. Redirecting robots.txt to another path loses the exception.
    """
    parsed = urlsplit(request.url)
    return bool(
        config.get("robots")
        and request.meta.get("_seal_robots_policy")
        and request.meta.get("seal_role") == "robots"
        and parsed.scheme in {"http", "https"}
        and parsed.hostname in config["allowed_hosts"]
        and parsed.path == "/robots.txt"
        and not parsed.query
        and not parsed.fragment
        and request.url == request.meta.get("_seal_robots_url")
    )
