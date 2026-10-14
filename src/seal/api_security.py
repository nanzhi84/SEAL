"""Private caller configuration and authenticated, opaque API capabilities."""

import base64
import binascii
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .core import SealError, canonical


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Caller(Strict):
    id: str = Field(min_length=1, max_length=100)
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: list[str] = Field(max_length=10000)

    @field_validator("sources")
    @classmethod
    def source_ids(cls, values):
        import re

        if any(not re.fullmatch(r"dd-[0-9]{3,}", value) for value in values):
            raise ValueError("invalid_source_id")
        return values

    def authorize(self, source_id):
        if source_id not in self.sources:
            raise ApiError(403, "source_forbidden")


class Settings(Strict):
    public_base_url: str = Field(max_length=2000)
    token_key: str
    callers: list[Caller] = Field(min_length=1, max_length=1000)
    workers: int = Field(default=4, ge=1, le=8)
    request_timeout_seconds: int = Field(default=30, ge=1, le=60)
    result_max_bytes: int = Field(default=16777216, ge=1024, le=67108864)
    attachment_max_bytes: int = Field(default=52428800, ge=1024, le=52428800)
    page_max_bytes: int = Field(default=67108864, ge=1024, le=134217728)

    @field_validator("public_base_url")
    @classmethod
    def base_url(cls, value):
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or any(ord(ch) < 33 or ord(ch) == 127 for ch in value)
        ):
            raise ValueError("invalid_public_base_url")
        _ = parts.port
        return value.rstrip("/")

    @field_validator("token_key")
    @classmethod
    def encryption_key(cls, value):
        Fernet(value)
        return value

    @model_validator(mode="after")
    def unique_callers(self):
        if len({c.id for c in self.callers}) != len(self.callers) or len(
            {c.key_sha256 for c in self.callers}
        ) != len(self.callers):
            raise ValueError("duplicate_caller")
        return self

    @classmethod
    def load(cls, path):
        try:
            return cls.model_validate_json(Path(path).read_bytes())
        except (OSError, ValueError, TypeError):
            raise SealError("invalid_api_config") from None

    def authenticate(self, authorization):
        scheme, _, key = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not key or len(key) > 4096 or key.isspace():
            raise ApiError(401, "unauthenticated")
        supplied = hashlib.sha256(key.encode()).hexdigest()
        caller = None
        for candidate in self.callers:
            if hmac.compare_digest(supplied, candidate.key_sha256):
                caller = candidate
        if caller is None:
            raise ApiError(401, "unauthenticated")
        return caller


class Cursor(Strict):
    purpose: str = Field(default="cursor", pattern="^cursor$")
    caller: str
    source: str
    watermark: int = Field(ge=0)
    after: str = Field(min_length=1, max_length=1000)
    issued_at: int = Field(ge=0)


class Attachment(Strict):
    purpose: str = Field(default="attachment", pattern="^attachment$")
    source: str
    version: str
    index: int = Field(ge=0)


class Tokens:
    def __init__(self, settings):
        self.cipher = Fernet(settings.token_key)

    def encode(self, payload):
        issued_at = payload.issued_at if isinstance(payload, Cursor) else int(time.time())
        return self.cipher.encrypt_at_time(canonical(payload.model_dump()), issued_at).decode()

    def decode(self, value, model):
        status, code = (400, "invalid_cursor") if model is Cursor else (404, "attachment_not_found")
        try:
            if not value or len(value) > 4096:
                raise ValueError
            encoded = value.encode("ascii")
            if (
                base64.urlsafe_b64encode(base64.b64decode(encoded, altchars=b"-_", validate=True))
                != encoded
            ):
                raise ValueError
            raw = self.cipher.decrypt(encoded, ttl=3600 if model is Cursor else 86400)
            payload = model.model_validate(json.loads(raw))
            if isinstance(payload, Cursor) and payload.issued_at != self.cipher.extract_timestamp(
                value.encode("ascii")
            ):
                raise ValueError
            return payload
        except (InvalidToken, UnicodeError, ValueError, ValidationError, binascii.Error):
            raise ApiError(status, code) from None
