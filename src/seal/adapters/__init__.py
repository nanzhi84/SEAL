"""Source Adapter contract: plan / parse / build / plan_recheck over archived inputs.

Adapters never perform I/O or write the database. They describe requests and document
targets, read already archived inputs through the Runtime context and return candidates.
"""

from importlib import import_module

from ..core import SealError

MODULES = {
    "web": "seal.adapters.web",
    "vietnam_law": "seal.adapters.vietnam_law",
    "federal_register": "seal.adapters.federal_register",
}


def load(name):
    if name not in MODULES:
        raise SealError("unknown_adapter")
    try:
        return import_module(MODULES[name]).Adapter
    except ModuleNotFoundError:
        raise SealError("adapter_not_implemented") from None


def contract(name):
    return load(name).CONTRACT


def validate_options(name, options):
    adapter = load(name)
    try:
        adapter.validate_options(options)
    except (ValueError, TypeError, KeyError):
        raise SealError("invalid_adapter_options") from None


class Base:
    """Shared defaults; subclasses define CONTRACT and the parsing rules."""

    CONTRACT = "seal.adapter.base.v1"

    def __init__(self, config):
        self.config = config

    @staticmethod
    def validate_options(options):
        if options:
            raise ValueError("unexpected_adapter_options")

    def plan(self, state):
        """Initial requests and targets for a collect Run; ``state`` is adapter_state."""
        return {
            "requests": [
                {"url": url, "role": "page", "depth": 0} for url in self.config["entry_urls"]
            ],
            "targets": [],
            "state": state,
        }

    def plan_recheck(self, resources, state):
        """Due Resources (stable identity, public_url, recheck_locator, current Version)."""
        targets = []
        for resource in resources:
            locator = resource["recheck_locator"]
            if not locator.get("url"):
                raise SealError("recheck_locator_unsupported")
            request = {"url": locator["url"], "role": locator.get("role", "page"), "depth": 0}
            # The known identity is registered up front, so a failed recheck is still
            # recorded as a failed delivery for that Resource.
            targets.append(
                {
                    "identity": {
                        "namespace": resource["namespace"],
                        "external_key": resource["external_key"],
                        "public_url": resource["public_url"],
                        "recheck_locator": locator,
                    },
                    "inputs": [{"request": request, "role": "detail"}],
                }
            )
        return {"requests": [], "targets": targets, "state": state}

    def parse(self, ctx, task, acquisition):
        raise NotImplementedError

    def build(self, ctx, target, inputs):
        raise NotImplementedError
