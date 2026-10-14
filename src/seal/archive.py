"""Complete SDK results; only original downloaded files are externalized to CAS."""

import json
from pathlib import Path

from pydantic import TypeAdapter

from .core import SENSITIVE, SealError
from .objects import Objects

json_envelope = TypeAdapter(dict)


def serialize_result(result, *, attachment_refs=()):
    data = result.model_dump(mode="python")
    if any(data.get(field) is not None for field in ("pdf", "screenshot", "mhtml")):
        raise SealError("page_artifact_generation_disabled")
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
    # The envelope carries storage encoding only; result keys retain SDK semantics.
    # SDK PDF metadata may contain datetimes. After externalizing downloads,
    # use Pydantic's JSON encoding instead of requiring Python-native values.
    return json_envelope.dump_python(
        {
            "result": data,
            "download_encoding": "sha256-cas",
            "attachment_refs": sorted(set(attachment_refs) | set(downloads)),
        },
        mode="json",
    )


def verify_result(objects, ref, *, max_bytes=None, attachment_max_bytes=None, deadline=None):
    try:
        envelope = json.loads(objects.get(ref, max_bytes=max_bytes, deadline=deadline))
    except (ValueError, TypeError):
        raise SealError("crawl_result_invalid") from None
    if (
        not isinstance(envelope, dict)
        or not isinstance(envelope.get("result"), dict)
        or envelope.get("download_encoding") != "sha256-cas"
    ):
        raise SealError("crawl_result_invalid")
    refs = envelope.get("attachment_refs")
    downloads = envelope["result"].get("downloaded_files") or []
    if (
        not isinstance(refs, list)
        or not isinstance(downloads, list)
        or any(not isinstance(ref, str) for ref in refs)
        or len(set(refs)) != len(refs)
        or any(key not in refs for key in downloads)
    ):
        raise SealError("crawl_result_invalid")
    for key in refs:
        objects.get(key, max_bytes=attachment_max_bytes, deadline=deadline)
    return envelope


def deserialize_result(envelope, *, download_directory=None):
    from crawl4ai import CrawlResult

    objects = Objects()
    for key in envelope.get("attachment_refs", []):
        objects.get(key)
    data = dict(envelope["result"])
    data["downloaded_files"] = [
        str(objects.materialize(key, download_directory))
        for key in data.get("downloaded_files") or []
    ] or None
    return CrawlResult(**data)
