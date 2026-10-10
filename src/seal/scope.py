"""One host/path boundary shared by configuration and the downloader guard."""

import posixpath
from urllib.parse import unquote


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
