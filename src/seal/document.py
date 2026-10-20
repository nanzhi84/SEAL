"""seal.inputs.v1 / seal.document.v1 contracts, four fingerprints and the 16-column rows.

Adapters return plain candidates; only this boundary turns them into immutable CAS
objects (Markdown bytes, input manifest, unified document) and delivery rows.
"""

import re
import unicodedata
from datetime import date, datetime, timezone

from .config import DOCUMENT_TYPES
from .core import SealError, canonical, digest
from .markdown import normalized
from .parsing import effective, fingerprint

DOCUMENT_SCHEMA = "seal.document.v1"
INPUTS_SCHEMA = "seal.inputs.v1"
SOURCE_RECORD_ALGORITHM = "seal.source-record.v1"
COLUMNS = (
    "资料名称",
    "解析后文件",
    "资料类型",
    "文件哈希",
    "采集时间",
    "原始文件",
    "文件顺序",
    "文号/案号",
    "发布机构",
    "发布日期",
    "生效日期",
    "失效日期",
    "状态",
    "来源名称",
    "来源详情页",
    "来源记录号",
)
INPUT_ROLES = {
    "primary",
    "volume",
    "independent_attachment",
    "source_response",
    "derived",
    "context",
}
DELIVERABLE_ROLES = ("primary", "volume", "independent_attachment")
EXTENSIONS = {
    "pdf": ("pdf", "application/pdf"),
    "doc": ("doc", "application/msword"),
    "docx": (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "rtf": ("rtf", "application/rtf"),
    "xls": ("xls", "application/vnd.ms-excel"),
    "xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "zip": ("zip", "application/zip"),
    "html": ("html", "text/html"),
    "txt": ("txt", "text/plain"),
    "json": ("json", "application/json"),
    "xml": ("xml", "application/xml"),
}
METADATA_FIELDS = (
    "title",
    "document_type",
    "document_number",
    "issuer",
    "status",
    "source_name",
    "language",
    "jurisdiction",
)
DATE_FIELDS = ("published_at", "effective_at", "expired_at")
MAX_TEXT = 4000


def source_record_hash(source_id, namespace, external_key):
    return digest(
        {
            "algorithm": SOURCE_RECORD_ALGORITHM,
            "source_id": source_id,
            "namespace": namespace,
            "external_key": external_key,
        }
    )


def safe_name(title):
    value = unicodedata.normalize("NFC", title)
    value = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    while len(value.encode("utf-8")) > 150:
        value = value[:-1]
    return value.rstrip(" ._") or "document"


def file_names(title, file_hash, extension, volume_order):
    suffix = "_%02d" % volume_order if volume_order else ""
    stem = safe_name(title) + "_" + file_hash[:8] + suffix
    return stem + ".md", (stem + "." + extension) if extension else None


def text(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise SealError("invalid_document_" + field)
    value = unicodedata.normalize("NFC", re.sub(r"\s+", " ", value)).strip()
    if len(value) > MAX_TEXT:
        raise SealError("invalid_document_" + field)
    return value or None


def iso_date(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SealError("invalid_document_" + field)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise SealError("invalid_document_" + field) from None
    return value


def timestamp(value):
    """采集时间: UTC, YYYY-MM-DD HH:MM:SS."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def sha(value, code):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SealError(code)
    return value


def normalize_metadata(candidate):
    meta = candidate.get("metadata") or {}
    if set(meta) - set(METADATA_FIELDS):
        raise SealError("invalid_document_metadata")
    metadata = {k: text(meta.get(k), k) for k in METADATA_FIELDS}
    if not metadata["title"]:
        raise SealError("document_title_missing")
    if metadata["document_type"] not in DOCUMENT_TYPES:
        raise SealError("invalid_document_type")
    dates = candidate.get("dates") or {}
    if set(dates) - set(DATE_FIELDS):
        raise SealError("invalid_document_dates")
    return metadata, {k: iso_date(dates.get(k), k) for k in DATE_FIELDS}


def normalize_inputs(candidate, acquisitions):
    inputs = []
    for item in candidate.get("inputs") or []:
        if item.get("role") not in INPUT_ROLES:
            raise SealError("invalid_input_role")
        acquisition = acquisitions.get(item.get("acquisition_id"))
        if acquisition is None or acquisition["state"] != "archived":
            raise SealError("input_acquisition_not_archived")
        if item.get("archive_ref") != acquisition["archive_ref"] and item.get("role") != "derived":
            raise SealError("input_archive_mismatch")
        locator = item.get("locator")
        if not isinstance(locator, dict) or not locator:
            raise SealError("input_locator_required")
        inputs.append(
            {
                "acquisition_id": item["acquisition_id"],
                "archive_ref": sha(item["archive_ref"], "invalid_input_archive_ref"),
                "role": item["role"],
                "locator": locator,
                "semantic_locator": item.get("semantic_locator") or locator,
                "slice_hash": sha(item.get("slice_hash") or item["archive_ref"], "invalid_slice"),
                "charset": item.get("charset"),
                "base_url": item.get("base_url"),
                "volume_order": item.get("volume_order"),
                "context": item.get("context") or {},
                "acquired_at": timestamp(acquisition["acquired_at"]),
            }
        )
    if not inputs:
        raise SealError("document_inputs_required")
    return inputs


def check_volumes(deliverables):
    volumes = [d for d in deliverables if d["role"] == "volume"]
    if any(d.get("volume_order") is None for d in volumes):
        raise SealError("volume_order_ambiguous")
    orders = sorted(d["volume_order"] for d in volumes)
    if orders != list(range(1, len(orders) + 1)):
        raise SealError("volume_order_ambiguous")
    if volumes and any(d["role"] == "primary" for d in deliverables):
        raise SealError("volume_and_primary_conflict")
    if sum(d["role"] == "primary" for d in deliverables) > 1:
        raise SealError("multiple_primary_deliverables")


def build(objects, *, source_id, run_id, manifest, candidate, acquisitions):
    """Validate a candidate, write Markdown/manifest/document to CAS, return rows."""
    namespace = text(candidate.get("namespace"), "namespace")
    external_key = candidate.get("external_key")
    if not namespace or not isinstance(external_key, str) or not external_key.strip():
        raise SealError("document_identity_required")
    metadata, dates = normalize_metadata(candidate)
    inputs = normalize_inputs(candidate, acquisitions)
    record = source_record_hash(source_id, namespace, external_key)
    raw = candidate.get("deliverables") or []
    if not raw:
        raise SealError("document_deliverables_required")
    for item in raw:
        if item.get("role") not in DELIVERABLE_ROLES:
            raise SealError("invalid_deliverable_role")
        if item["role"] != "volume" and item.get("volume_order") is not None:
            raise SealError("volume_order_without_volume")
    check_volumes(raw)
    ordered = sorted(
        raw,
        key=lambda d: (
            DELIVERABLE_ROLES.index(d["role"]),
            d.get("volume_order") or 0,
        ),
    )
    deliverables, rows, files, names = [], [], [], set()
    for index, item in enumerate(ordered, 1):
        body = normalized(item.get("markdown") or "")
        if not body.strip():
            raise SealError("deliverable_markdown_empty")
        data = body.encode("utf-8")
        markdown_ref = objects.put(data)
        objects.get(markdown_ref)
        original = item.get("original")
        if original is not None:
            sha(original.get("ref"), "invalid_original_ref")
            if original.get("kind") not in EXTENSIONS:
                raise SealError("invalid_original_kind")
        position = item.get("input")
        source = inputs[position] if type(position) is int and 0 <= position < len(inputs) else None
        if source is None:
            raise SealError("deliverable_input_required")
        if original and original["ref"] != source["archive_ref"]:
            raise SealError("deliverable_original_input_mismatch")
        title = text(item.get("title"), "title") or metadata["title"]
        if item["role"] == "independent_attachment" and not text(item.get("title"), "title"):
            raise SealError("attachment_title_missing")
        file_hash = original["ref"] if original else markdown_ref
        extension, media_type = EXTENSIONS[original["kind"]] if original else (None, None)
        markdown_file, original_file = file_names(
            title, file_hash, extension, item.get("volume_order")
        )
        if markdown_file in names:
            raise SealError("duplicate_deliverable_file")
        names.add(markdown_file)
        shared = item["role"] != "independent_attachment"
        overrides = item.get("metadata") or {}
        row_meta = {
            k: text(overrides.get(k), k) if k in overrides else (metadata[k] if shared else None)
            for k in ("document_number", "issuer", "status")
        }
        row_dates = {
            k: iso_date(overrides.get(k), k) if k in overrides else (dates[k] if shared else None)
            for k in DATE_FIELDS
        }
        deliverable = {
            "index": index,
            "role": item["role"],
            "title": title,
            "volume_order": item.get("volume_order"),
            "markdown_ref": markdown_ref,
            "original_file_ref": original["ref"] if original else None,
            "file_hash": file_hash,
            "file_name": original_file,
            "markdown_file_name": markdown_file,
            "media_type": media_type,
            "extension": extension,
            "quality_report": item.get("quality") or {},
            "extraction": item.get("extraction") or {},
            "locator": source["locator"],
            "acquisition_id": source["acquisition_id"],
            "metadata": {**row_meta, **row_dates},
        }
        deliverables.append(deliverable)
        if original:
            files.append(
                {
                    "ref": original["ref"],
                    "role": item["role"],
                    "original_name": original.get("original_name"),
                    "media_type": media_type,
                    "byte_size": original.get("byte_size"),
                    "source_url": original.get("source_url"),
                    "volume_order": item.get("volume_order"),
                    "locator": source["locator"],
                }
            )
        rows.append(
            {
                "row_number": index,
                "title": title,
                "markdown_file": markdown_file,
                "document_type": metadata["document_type"],
                "file_hash": file_hash,
                "acquired_at": source["acquired_at"],
                "original_file": original_file,
                "volume_order": item.get("volume_order"),
                "document_number": row_meta["document_number"],
                "issuer": row_meta["issuer"],
                "published_at": row_dates["published_at"],
                "effective_at": row_dates["effective_at"],
                "expired_at": row_dates["expired_at"],
                "status": row_meta["status"],
                "source_name": metadata["source_name"],
                "detail_url": candidate.get("public_url"),
                "source_record_hash": record,
                "markdown_hash": markdown_ref,
                "original_ref": original["ref"] if original else None,
                "original_media_type": media_type,
                "role": item["role"],
                "locator": source["locator"],
            }
        )
    for item in inputs:
        objects.get(item["archive_ref"])
    used = set(candidate.get("parsers") or [])
    for item in ordered:
        used.update(item.get("parsers") or [])
    parser_config = effective(manifest, used)
    semantic_inputs = sorted(
        (
            {
                k: item[k]
                for k in (
                    "role",
                    "semantic_locator",
                    "slice_hash",
                    "charset",
                    "base_url",
                    "volume_order",
                    "context",
                )
            }
            for item in inputs
        ),
        key=canonical,
    )
    input_fp = fingerprint(
        "input",
        {"namespace": namespace, "external_key": external_key, "inputs": semantic_inputs},
    )
    parser_fp = fingerprint("parser", parser_config)
    version_fp = fingerprint("version", {"input": input_fp, "parser": parser_fp})
    business = {
        "metadata": metadata,
        "dates": dates,
        "deliverables": [
            {
                k: d[k]
                for k in (
                    "role",
                    "title",
                    "volume_order",
                    "markdown_ref",
                    "original_file_ref",
                    "file_hash",
                    "metadata",
                )
            }
            for d in deliverables
        ],
        "tables": candidate.get("tables") or [],
        "links": candidate.get("links") or [],
        "media": candidate.get("media") or [],
        "extensions": candidate.get("extensions") or {},
    }
    content_hash = fingerprint("content", business)
    manifest_doc = {
        "schema": INPUTS_SCHEMA,
        "source_id": source_id,
        "run_id": run_id,
        "document": {"namespace": namespace, "external_key": external_key},
        "inputs": inputs,
        "adapter_inputs": candidate.get("adapter_inputs") or [],
        "input_reuse": candidate.get("input_reuse"),
    }
    inputs_ref = objects.put_json(manifest_doc)
    acquired = max(item["acquired_at"] for item in inputs)
    document = {
        "schema": DOCUMENT_SCHEMA,
        "identity": {
            "source_id": source_id,
            "namespace": namespace,
            "external_key": external_key,
            "public_url": candidate.get("public_url"),
            "source_record_hash": record,
        },
        "metadata": metadata,
        "dates": {**dates, "acquired_at": acquired},
        "content": {
            "markdown": normalized(ordered[0]["markdown"]),
            "markdown_hash": deliverables[0]["markdown_ref"],
        },
        "deliverables": deliverables,
        "extraction": {
            "adapter": parser_config["adapter"],
            "markdown_contract": parser_config["markdown"]["contract"],
            "parsers": sorted(used),
            "deliverables": [
                {"index": d["index"], **d["extraction"], "quality_report": d["quality_report"]}
                for d in deliverables
            ],
        },
        "tables": business["tables"],
        "links": business["links"],
        "media": business["media"],
        "files": files,
        "lineage": {"inputs_ref": inputs_ref, "native_result_ref": None},
        "extensions": business["extensions"],
    }
    result_ref = objects.put_json(document)
    objects.get(result_ref)
    objects.get(inputs_ref)
    return {
        "namespace": namespace,
        "external_key": external_key,
        "public_url": candidate.get("public_url"),
        "recheck_locator": candidate.get("recheck_locator") or {"url": candidate.get("public_url")},
        "result_ref": result_ref,
        "inputs_ref": inputs_ref,
        "input_fingerprint": input_fp,
        "parser_fingerprint": parser_fp,
        "version_fingerprint": version_fp,
        "content_hash": content_hash,
        "parser_config": parser_config,
        "rows": rows,
        "acquisition_ids": sorted({item["acquisition_id"] for item in inputs}),
    }


def verify_document(objects, result_ref, *, max_bytes=None, deadline=None):
    """Read-side validator: unified document plus its complete object closure."""
    try:
        import json

        value = json.loads(objects.get(result_ref, max_bytes=max_bytes, deadline=deadline))
    except (ValueError, TypeError):
        raise SealError("document_invalid") from None
    if not isinstance(value, dict) or value.get("schema") != DOCUMENT_SCHEMA:
        raise SealError("document_invalid")
    deliverables = value.get("deliverables")
    if not isinstance(deliverables, list) or not deliverables:
        raise SealError("document_invalid")
    for item in deliverables:
        objects.get(sha(item.get("markdown_ref"), "document_invalid"), deadline=deadline)
        if item.get("original_file_ref"):
            objects.get(sha(item["original_file_ref"], "document_invalid"), deadline=deadline)
    lineage = value.get("lineage") or {}
    manifest_raw = objects.get(
        sha(lineage.get("inputs_ref"), "document_invalid"), deadline=deadline
    )
    try:
        manifest = json.loads(manifest_raw)
        if not isinstance(manifest, dict) or manifest.get("schema") != INPUTS_SCHEMA:
            raise ValueError
        inputs = manifest["inputs"]
        if not isinstance(inputs, list) or not inputs:
            raise ValueError
        for item in inputs:
            objects.get(sha(item.get("archive_ref"), "document_invalid"), deadline=deadline)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SealError("document_invalid") from None
    return value


def row_values(row):
    """One delivery row as the 16 template columns (strings; missing values empty)."""

    def day(value):
        if value is None:
            return ""
        return value.isoformat() if isinstance(value, date) else str(value)

    return {
        "资料名称": row["title"],
        "解析后文件": row["markdown_file"],
        "资料类型": row["document_type"],
        "文件哈希": row["file_hash"],
        "采集时间": timestamp(row["acquired_at"]),
        "原始文件": row["original_file"] or "",
        "文件顺序": str(row["volume_order"]) if row["volume_order"] else "",
        "文号/案号": row["document_number"] or "",
        "发布机构": row["issuer"] or "",
        "发布日期": day(row["published_at"]),
        "生效日期": day(row["effective_at"]),
        "失效日期": day(row["expired_at"]),
        "状态": row["status"] or "",
        "来源名称": row["source_name"] or "",
        "来源详情页": row["detail_url"] or "",
        "来源记录号": row["source_record_hash"],
    }
