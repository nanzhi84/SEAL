"""Serialize CrawlResult without an AST; binary outputs and downloads use durable CAS."""

import base64
from pathlib import Path

from pydantic import TypeAdapter

from .core import SENSITIVE, SealError
from .objects import Objects

json_envelope = TypeAdapter(dict)


def serialize_result(result):
    data = result.model_dump(mode="python")
    objects = Objects()
    if data.get("response_headers"):
        data["response_headers"] = {
            k: ("REDACTED" if SENSITIVE.search(k) or k.lower() in ("set-cookie", "cookie") else v)
            for k, v in data["response_headers"].items()
        }
    downloads = []
    for name in data.get("downloaded_files") or []:
        try:
            key = objects.put(Path(name).read_bytes())
        except OSError:
            raise SealError("download_archive_failed") from None
        downloads.append(key)
    data["downloaded_files"] = downloads or None
    artifacts = {}
    for field in ("pdf", "screenshot", "mhtml"):
        value = data.get(field)
        if value is not None:
            if field == "screenshot":
                body = base64.b64decode(value, validate=True)
            else:
                body = value if isinstance(value, bytes) else value.encode()
            artifacts[field] = objects.put(body)
            data[field] = None
    # The envelope carries storage encoding only; result keys retain SDK semantics.
    # SDK PDF metadata may contain datetimes. After externalizing binary fields,
    # use Pydantic's JSON encoding instead of requiring Python-native values.
    return json_envelope.dump_python(
        {"result": data, "artifacts": artifacts, "download_encoding": "sha256-cas"}, mode="json"
    )


def deserialize_result(envelope, *, download_directory=None):
    from crawl4ai import CrawlResult

    objects = Objects()
    data = dict(envelope["result"])
    for field, key in envelope.get("artifacts", {}).items():
        body = objects.get(key)
        data[field] = (
            body
            if field == "pdf"
            else (base64.b64encode(body).decode() if field == "screenshot" else body.decode())
        )
    data["downloaded_files"] = [
        str(objects.materialize(key, download_directory))
        for key in data.get("downloaded_files") or []
    ] or None
    return CrawlResult(**data)
