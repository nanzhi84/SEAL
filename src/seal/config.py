"""Closed Source configuration; historical Recipe YAML requires explicit migration."""

from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .core import SealError, check_address, public_url
from .scope import paths_for, valid_prefix


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
            value = yaml.load(file, Loader=UniqueLoader)
        if not isinstance(value, dict):
            raise SealError("invalid_source_config")
        if "recipe" in value:
            raise SealError("legacy_recipe_config_requires_migration")
        return SourceConfig.model_validate(value)
    except (yaml.YAMLError, ValidationError, OSError):
        raise SealError("invalid_source_config") from None


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Budget(Strict):
    requests: int = Field(default=100, ge=1, le=10000)
    seconds: int = Field(default=120, ge=1, le=3600)
    response_bytes: int = Field(default=10485760, ge=1024, le=52428800)


class CrawlConfig(Strict):
    mode: str = Field(default="http", pattern="^(http|rendered_dom)$")
    max_depth: int = Field(default=3, ge=0, le=100)
    page_timeout_ms: int = Field(default=30000, ge=100, le=300000)
    word_count_threshold: int = Field(default=1, ge=0, le=10000)
    css_selector: str | None = Field(default=None, max_length=1000)
    wait_until: str = Field(default="domcontentloaded", pattern="^(commit|domcontentloaded|load)$")


class SourceConfig(Strict):
    id: str = Field(pattern=r"^dd-[0-9]{3,}$")
    entry_urls: list[str] = Field(min_length=1, max_length=1000)
    allowed_hosts: list[str] = Field(min_length=1)
    allowed_path_prefixes: list[str] = Field(default_factory=lambda: ["/"], min_length=1)
    host_path_scopes: dict[str, list[str]] = Field(default_factory=dict)
    archive_approved: bool
    scope: str = Field(min_length=5, max_length=2000)
    robots: bool = True
    robots_redirect_urls: list[str] = Field(default_factory=list, max_length=20)
    user_agent: str = Field(default="SEAL/0.3 (+authorized archival crawler)", max_length=256)
    recheck_seconds: int = Field(default=86400, ge=0, le=31536000)
    concurrency: int = Field(default=2, ge=1, le=8)
    delay: float = Field(default=0.1, ge=0.0, le=60.0)
    budget: Budget = Field(default_factory=Budget)
    crawl4ai: CrawlConfig = Field(default_factory=CrawlConfig)
    blocked_patterns: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("entry_urls")
    @classmethod
    def urls(cls, values):
        return list(dict.fromkeys(public_url(url) for url in values))

    @field_validator("robots_redirect_urls")
    @classmethod
    def policy_urls(cls, values):
        urls = list(dict.fromkeys(public_url(url) for url in values))
        for url in urls:
            check_address(urlsplit(url).hostname)
        return urls

    @model_validator(mode="after")
    def boundaries(self):
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
        for host in self.allowed_hosts:
            check_address(host)
        for url in self.entry_urls:
            parsed = urlsplit(url)
            if not paths_for(self.model_dump(), parsed.hostname, parsed.path):
                raise SealError("source_out_of_scope")
        return self
