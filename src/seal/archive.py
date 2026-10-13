"""Complete SDK results; only original downloaded files are externalized to CAS."""

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


def verify_result(objects, ref):
    envelope = objects.json(ref)
    if (
        not isinstance(envelope, dict)
        or not isinstance(envelope.get("result"), dict)
        or envelope.get("download_encoding") != "sha256-cas"
    ):
        raise SealError("crawl_result_invalid")
    for key in envelope["result"].get("downloaded_files") or []:
        objects.get(key)
    for key in envelope.get("attachment_refs", []):
        objects.get(key)
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
