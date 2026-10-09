import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .core import SealError, public_url


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    data = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in data:
            raise SealError("duplicate_config_key")
        data[key] = loader.construct_object(value_node, deep=deep)
    return data


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_file(path):
    try:
        with open(path) as file:
            return yaml.load(file, Loader=UniqueLoader)
    except yaml.YAMLError:
        raise SealError("invalid_yaml") from None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Budget(Strict):
    requests: int = Field(default=100, ge=1, le=10000)
    seconds: int = Field(default=120, ge=1, le=3600)
    response_bytes: int = Field(default=10485760, ge=1024, le=52428800)


class SourceConfig(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    entry_urls: list[str] = Field(min_length=1, max_length=1000)
    allowed_hosts: list[str] = Field(min_length=1)
    allowed_path_prefixes: list[str] = Field(min_length=1)
    # When present, paths belong to a particular host, never to the union of hosts.
    host_path_scopes: dict[str, list[str]] = Field(default_factory=dict)
    research_ids: list[str] = Field(default_factory=list, max_length=111)
    methods: list[Literal["GET", "HEAD"]] = ["GET", "HEAD"]
    identity: Literal["canonical_url", "business_key"] = "canonical_url"
    output_schema: Literal["generic_document.v1", "record.v1"] = "generic_document.v1"
    archive_approved: bool
    scope: str = Field(min_length=5, max_length=2000)
    seed_role: Literal["list", "detail", "api", "iframe", "attachment"] = "list"
    poll_seconds: int = Field(default=3600, ge=60)
    recheck_seconds: int = Field(default=86400, ge=60)
    budget: Budget = Budget()
    concurrency: int = Field(default=2, ge=1, le=8)
    delay: float = Field(default=0.1, ge=0.0, le=60.0)
    # Historical external configuration remains readable; Runtime never enables it.
    robots: bool = False
    user_agent: str = "SEAL/0.1 (+authorized archival crawler)"

    @field_validator("entry_urls")
    @classmethod
    def urls(cls, values):
        return [public_url(u) for u in values]

    @model_validator(mode="after")
    def check_scope(self):
        from urllib.parse import urlsplit

        from .scope import paths_for, valid_prefix

        if not self.archive_approved:
            raise SealError("source_archive_approval_required")
        if any(not valid_prefix(p) for p in self.allowed_path_prefixes):
            raise SealError("invalid_path_scope")
        if self.host_path_scopes and (
            set(self.host_path_scopes) != set(self.allowed_hosts)
            or any(
                not paths or any(not valid_prefix(p) for p in paths)
                for paths in self.host_path_scopes.values()
            )
        ):
            raise SealError("invalid_host_path_scope")
        if len(set(self.research_ids)) != len(self.research_ids) or any(
            re.fullmatch(r"dd-[0-9]{3}", identity) is None
            for identity in self.research_ids
        ):
            raise SealError("invalid_research_id")
        for url in self.entry_urls:
            parsed = urlsplit(url)
            if not paths_for(self.model_dump(), parsed.hostname, parsed.path):
                raise SealError("source_out_of_scope")
        return self

    def execution(self):
        return self.model_dump(exclude={"poll_seconds", "recheck_seconds"})
