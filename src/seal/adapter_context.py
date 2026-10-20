"""Adapter capabilities and stable task identities; no transport access is exposed."""

from .core import SealError, digest
from .objects import Objects
from .pdf_runtime import run_extraction
from .quality import text_check
from .transport import request_key


def adapter_error(exc, prefix):
    """Stable error code; source-specific or unexpected failures never crash the Run."""
    if isinstance(exc, SealError):
        if exc.code in {
            "interrupted",
            "crawl_time_budget_exceeded",
            "stale_attempt",
            "inactive_attempt",
            "run_not_started",
            "deadline_exceeded",
        }:
            raise exc
        return exc.code
    return prefix + ":" + type(exc).__name__


def target_key(target):
    identity = target.get("identity") or {}
    if identity.get("namespace") and identity.get("external_key"):
        return digest(
            {"namespace": identity["namespace"], "external_key": identity["external_key"]}
        )
    return digest({"inputs": [request_key(item["request"]) for item in target["inputs"]]})


class Context:
    """The only capabilities an Adapter receives: archived reads and bounded extraction."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.config = runtime.config
        self.objects = Objects()

    def read(self, acquisition):
        if acquisition.get("state") != "archived":
            raise SealError("input_not_archived")
        return self.objects.get(acquisition["archive_ref"])

    def store(self, data):
        ref = self.objects.put(data)
        self.objects.get(ref)
        return ref

    def extract(self, data, kind):
        return run_extraction(data, kind, self.config, active=self.runtime.budget.active)

    def text_quality(self, markdown):
        language = str(self.config["metadata"].get("language") or "")
        return text_check(markdown, "vi" if language.startswith("vi") else "generic")

    def follow_links(self, task):
        return self.runtime.mode == "collect" and task["depth"] < self.config["fetch"]["max_depth"]
