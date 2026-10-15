"""SDK archive to public content; explicit whitelists, never SDK passthrough."""

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .api_security import Attachment
from .core import SealError


class Content(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class Metadata(Content):
    title: str | None = None
    author: str | None = None
    description: str | None = None
    keywords: str | None = None
    pdf: "PDF | None" = None


class PDFPage(Content):
    page_number: int = Field(ge=1)
    text: str


class OCR(Content):
    engine: str
    version: str
    config: "OCRSettings"


class OCRSettings(Content):
    languages: str
    timeout_seconds: int
    max_pages: int
    dpi: int
    max_page_pixels: int
    max_result_bytes: int


class PDF(Content):
    method: str = Field(pattern="^(native|ocr)$")
    pages: list[PDFPage]
    ocr: OCR | None = None
    original_attachment_index: int | None = Field(default=None, ge=0)


class Link(Content):
    href: str | None = None
    text: str | None = None
    title: str | None = None


class Links(Content):
    internal: list[Link] = Field(default_factory=list)
    external: list[Link] = Field(default_factory=list)


class MediaItem(Content):
    src: str | None = None
    alt: str | None = None
    desc: str | None = None
    type: str | None = None
    format: str | None = None
    width: int | None = Field(default=None, ge=0)

    @field_validator("width", mode="before")
    @classmethod
    def sdk_width(cls, value):
        if isinstance(value, str) and value.isascii() and value.isdigit():
            return int(value)
        return value


class Media(Content):
    images: list[MediaItem] = Field(default_factory=list)
    videos: list[MediaItem] = Field(default_factory=list)
    audios: list[MediaItem] = Field(default_factory=list)


class Table(Content):
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    caption: str | None = None
    summary: str | None = None


def envelope(objects, ref, *, max_bytes, deadline):
    try:
        value = json.loads(objects.get(ref, max_bytes=max_bytes, deadline=deadline))
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("result"), dict)
            or value.get("download_encoding") != "sha256-cas"
            or not isinstance(value.get("attachment_refs"), list)
        ):
            raise ValueError
        refs = value["attachment_refs"]
        for key in refs:
            objects.key(key)
        if len(set(refs)) != len(refs):
            raise ValueError
        downloads = value["result"].get("downloaded_files") or []
        if not isinstance(downloads, list) or any(key not in refs for key in downloads):
            raise ValueError
        return value
    except (ValueError, TypeError, SealError) as exc:
        if isinstance(exc, SealError) and exc.code != "invalid_object_key":
            raise
        raise SealError("archive_invalid") from None


def project(row, value, settings, tokens):
    result = value["result"]
    try:
        markdown = result.get("markdown")
        if markdown is None:
            markdown = ""
        elif isinstance(markdown, dict):
            markdown = markdown["raw_markdown"]
        else:
            raise ValueError
        if not isinstance(markdown, str):
            raise ValueError
        metadata = result.get("metadata")
        public_metadata = Metadata.model_validate({} if metadata is None else metadata).model_dump()
        if public_metadata["pdf"] is not None:
            public_metadata["pdf"]["original_attachment_index"] = value["attachment_refs"].index(
                metadata["pdf"]["original_ref"]
            )
        links = result.get("links")
        media = result.get("media")
        tables = result.get("tables")
        if tables is not None and not isinstance(tables, list):
            raise ValueError
        return {
            "url": row["url"],
            "markdown": markdown,
            "metadata": public_metadata,
            "links": Links.model_validate({} if links is None else links).model_dump(),
            "media": Media.model_validate({} if media is None else media).model_dump(),
            "tables": [Table.model_validate(t).model_dump() for t in tables or []],
            "attachments": [
                {
                    "url": settings.public_base_url
                    + "/attachments/"
                    + tokens.encode(
                        Attachment(
                            source=row["source_id"],
                            version=row["version_id"],
                            index=i,
                            caller=row["caller_id"],
                            snapshot=row["snapshot_id"],
                            resource=row["resource_id"],
                        )
                    ),
                    "name": None,
                    "media_type": None,
                }
                for i, _ in enumerate(value["attachment_refs"])
            ],
        }
    except (ValueError, TypeError, KeyError, ValidationError):
        raise SealError("archive_invalid") from None
