import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .core import SENSITIVE, SealError, public_url


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


class AbsentRobotsResponse(Strict):
    """Explicitly reviewed missing policy, never a wildcard permission."""

    url: str
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("url")
    @classmethod
    def public(cls, value):
        return public_url(value)


class PublicPostRule(Strict):
    url: str
    fields: dict[str, Literal["page", "region", "query_id", "empty", "management", "date"]] = Field(
        min_length=1, max_length=20
    )

    @model_validator(mode="after")
    def public_query(self):
        self.url = public_url(self.url)
        if any(SENSITIVE.search(name) for name in self.fields):
            raise SealError("public_form_sensitive_field_rejected")
        return self


class FormSubmit(Strict):
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=1024)


class PublicHtmlFormRule(Strict):
    """Explicit public form: archived defaults, declared select overrides, fixed submit."""

    url: str
    parent_url: str
    form_xpath: str = Field(min_length=1, max_length=512)
    fields: list[str] = Field(min_length=1, max_length=100)
    select_fields: list[str] = Field(default_factory=list, max_length=20)
    submit: FormSubmit
    max_body_bytes: int = Field(default=131072, ge=1024, le=131072)

    @model_validator(mode="after")
    def public_form(self):
        from lxml import etree

        self.url, self.parent_url = public_url(self.url), public_url(self.parent_url)
        if (
            len(set(self.fields)) != len(self.fields)
            or len(set(self.select_fields)) != len(self.select_fields)
            or not set(self.select_fields) <= set(self.fields)
            or self.submit.name not in self.fields
            or self.submit.name in self.select_fields
            or any(not name.strip() or len(name) > 256 for name in self.fields)
        ):
            raise SealError("public_html_form_fields_invalid")
        if any(SENSITIVE.search(name) for name in self.fields):
            raise SealError("public_form_sensitive_field_rejected")
        try:
            etree.XPath(self.form_xpath)
        except etree.XPathError:
            raise SealError("public_html_form_xpath_invalid") from None
        return self


class SourceConfig(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    entry_urls: list[str] = Field(min_length=1, max_length=1000)
    allowed_hosts: list[str] = Field(min_length=1)
    allowed_path_prefixes: list[str] = Field(min_length=1)
    # When present, paths belong to a particular host, never to the union of hosts.
    host_path_scopes: dict[str, list[str]] = Field(default_factory=dict)
    research_ids: list[str] = Field(default_factory=list, max_length=111)
    methods: list[Literal["GET", "HEAD", "POST"]] = ["GET", "HEAD"]
    public_post_rules: list[PublicPostRule] = Field(default_factory=list, max_length=5)
    public_html_forms: list[PublicHtmlFormRule] = Field(default_factory=list, max_length=5)
    archive_approved: bool
    scope: str = Field(min_length=5, max_length=2000)
    seed_role: Literal["list", "detail", "api", "iframe", "attachment", "sitemap"] = "list"
    recheck_seconds: int = Field(default=86400, ge=60)
    budget: Budget = Budget()
    concurrency: int = Field(default=2, ge=1, le=8)
    delay: float = Field(default=0.1, ge=0.0, le=60.0)
    # Explicit opt-in keeps historical V1.2 bindings unchanged. Seeded recipes require it.
    robots: bool = False
    robots_redirect_paths: list[str] = Field(default_factory=list, max_length=5)
    robots_absent_responses: list[AbsentRobotsResponse] = Field(default_factory=list, max_length=5)
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
        if any(not valid_prefix(p) for p in self.robots_redirect_paths):
            raise SealError("invalid_robots_redirect_path")
        for missing in self.robots_absent_responses:
            parsed = urlsplit(missing.url)
            if (
                not self.robots
                or parsed.hostname not in self.allowed_hosts
                or parsed.query
                or parsed.path not in ["/robots.txt", *self.robots_redirect_paths]
            ):
                raise SealError("invalid_robots_absent_response")
        if self.host_path_scopes and (
            set(self.host_path_scopes) != set(self.allowed_hosts)
            or any(
                not paths or any(not valid_prefix(p) for p in paths)
                for paths in self.host_path_scopes.values()
            )
        ):
            raise SealError("invalid_host_path_scope")
        if len(set(self.research_ids)) != len(self.research_ids) or any(
            re.fullmatch(r"dd-[0-9]{3}", identity) is None for identity in self.research_ids
        ):
            raise SealError("invalid_research_id")
        for url in self.entry_urls:
            parsed = urlsplit(url)
            if not paths_for(self.model_dump(), parsed.hostname, parsed.path):
                raise SealError("source_out_of_scope")
        for rule in self.public_post_rules:
            parsed = urlsplit(rule.url)
            if (
                "POST" not in self.methods
                or parsed.query
                or not paths_for(self.model_dump(), parsed.hostname, parsed.path)
            ):
                raise SealError("public_form_out_of_scope")
        if len({rule.url for rule in self.public_post_rules}) != len(self.public_post_rules):
            raise SealError("duplicate_public_form_rule")
        for rule in self.public_html_forms:
            for url in (rule.url, rule.parent_url):
                parsed = urlsplit(url)
                if "POST" not in self.methods or not paths_for(
                    self.model_dump(), parsed.hostname, parsed.path
                ):
                    raise SealError("public_form_out_of_scope")
        form_urls = [rule.url for rule in (*self.public_post_rules, *self.public_html_forms)]
        if len(form_urls) != len(set(form_urls)):
            raise SealError("duplicate_public_form_rule")
        return self

    def execution(self):
        return self.model_dump(exclude={"recipe"})


class RecipeReference(Strict):
    path: str = Field(min_length=1)
    params: dict = Field(default_factory=dict)


class CollectionConfig(SourceConfig):
    recipe: RecipeReference
