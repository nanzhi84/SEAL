"""Task-local JSON Schemas and frozen trusted Python field rules; no business keys."""

import ast
import base64
import inspect
import re
from copy import deepcopy
from urllib.parse import urlsplit

import jsonschema
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable

from .core import SealError

CONTRACT_KEYS = {
    "extraction_schema",
    "data_schema",
    "field_mapping",
    "field_parsers",
    "field_transforms",
}
DIALECTS = {
    "https://json-schema.org/draft/2020-12/schema": jsonschema.Draft202012Validator,
    "https://json-schema.org/draft/2019-09/schema": jsonschema.Draft201909Validator,
}


def _no_retrieve(_uri):
    raise SealError("field_remote_schema_rejected")


def schema_validator(schema):
    if not isinstance(schema, dict) or schema.get("$schema") not in DIALECTS:
        raise SealError("field_schema_dialect_rejected")
    identity = schema.get("$id")
    if not isinstance(identity, str) or not urlsplit(identity).scheme:
        raise SealError("field_schema_identity_required")
    pending, references = [schema], []
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for name in ("$ref", "$dynamicRef", "$recursiveRef"):
                if name in node and (
                    not isinstance(node[name], str) or not node[name].startswith("#")
                ):
                    raise SealError("field_remote_schema_rejected")
                if name in node:
                    references.append(node[name])
            if "$schema" in node and node["$schema"] != schema["$schema"]:
                raise SealError("field_schema_dialect_rejected")
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    cls = DIALECTS[schema["$schema"]]
    try:
        cls.check_schema(schema)
        if (
            schema.get("type") != "object"
            or not isinstance(schema.get("properties"), dict)
            or not schema["properties"]
            or schema.get("additionalProperties") is not False
        ):
            raise SealError("field_schema_closed_object_required")
        if any(not key.strip() for key in schema["properties"]):
            raise SealError("field_schema_key_rejected")
        registry = Registry(retrieve=_no_retrieve).with_resource(
            identity, Resource.from_contents(schema)
        )
        for reference in references:
            try:
                registry.resolver(identity).lookup(reference)
            except Unresolvable:
                raise SealError("field_schema_reference_invalid") from None
        return cls(schema, registry=registry)
    except jsonschema.SchemaError:
        raise SealError("field_schema_invalid") from None


def _signature(node):
    args = node.args
    positional = args.posonlyargs + args.args
    defaults = [inspect.Parameter.empty] * (len(positional) - len(args.defaults)) + [None] * len(
        args.defaults
    )
    parameters = [
        inspect.Parameter(
            arg.arg,
            inspect.Parameter.POSITIONAL_ONLY
            if index < len(args.posonlyargs)
            else inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=defaults[index],
        )
        for index, arg in enumerate(positional)
    ]
    if args.vararg:
        parameters.append(inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    parameters.extend(
        inspect.Parameter(
            arg.arg,
            inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if default is None else None,
        )
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    )
    if args.kwarg:
        parameters.append(inspect.Parameter(args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(parameters)


def check_rule(rule, files):
    if (
        not isinstance(rule, dict)
        or set(rule) - {"entrypoint", "params"}
        or not isinstance(rule.get("entrypoint"), str)
        or not re.fullmatch(r"[a-zA-Z_][\w.]*:[A-Za-z_]\w*", rule["entrypoint"])
        or not isinstance(rule.get("params", {}), dict)
    ):
        raise SealError("field_rule_invalid")
    module, name = rule["entrypoint"].split(":")
    encoded = files.get(module.replace(".", "/") + ".py")
    if encoded is None:
        raise SealError("field_rule_entrypoint_missing")
    nodes = [
        node
        for node in ast.parse(base64.b64decode(encoded)).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(nodes) != 1:
        raise SealError("field_rule_entrypoint_missing")
    try:
        _signature(nodes[0]).bind(object(), **rule.get("params", {}))
    except (ValueError, TypeError):
        raise SealError("field_rule_parameters_invalid") from None
    # Configuration parameters obey the same strict JSON boundary as Record data.
    from .record_validation import json_value

    json_value(rule.get("params", {}))


def field_contract(manifest, files):
    """Validate at pack; the returned contract is reconstructed from the frozen bundle."""
    if not CONTRACT_KEYS & manifest.keys():
        return None
    if "extraction_schema" not in manifest:
        raise SealError("field_extraction_schema_required")
    extraction = manifest["extraction_schema"]
    schema_validator(extraction)
    keys = set(extraction["properties"])
    mapping = manifest.get("field_mapping", {})
    if not isinstance(mapping, dict) or not set(mapping) <= keys:
        raise SealError("field_mapping_source_invalid")
    if any(not isinstance(target, str) or not target.strip() for target in mapping.values()):
        raise SealError("field_mapping_target_invalid")
    targets = [mapping.get(key, key) for key in keys]
    if len(targets) != len(set(targets)):
        raise SealError("field_mapping_collision")
    contract = {"extraction_schema": extraction, "field_mapping": mapping}
    for name in ("field_parsers", "field_transforms"):
        rules = manifest.get(name, {})
        if not isinstance(rules, dict) or not set(rules) <= keys:
            raise SealError("field_rule_source_invalid")
        for rule in rules.values():
            check_rule(rule, files)
        contract[name] = rules
    internal = manifest.get("data_schema")
    if internal is None:
        if contract["field_transforms"]:
            raise SealError("field_transform_schema_required")
        if not mapping:
            internal = extraction
        else:
            # Only unambiguous root-key renames may be derived. Complex JSON Schema
            # composition/reference semantics need an explicit separately reviewed Schema.
            allowed = {
                "$schema",
                "$id",
                "type",
                "properties",
                "required",
                "additionalProperties",
                "title",
                "description",
            }
            if set(extraction) - allowed or _has_reference(extraction):
                raise SealError("field_explicit_data_schema_required")
            internal = deepcopy(extraction)
            internal["$id"] += ":mapped"
            internal["properties"] = {
                mapping.get(key, key): value for key, value in internal["properties"].items()
            }
            if "required" in internal:
                internal["required"] = [mapping.get(key, key) for key in internal["required"]]
    schema_validator(internal)
    if set(internal["properties"]) != set(targets):
        raise SealError("field_mapping_schema_mismatch")
    contract["data_schema"] = internal
    return contract


def _has_reference(node):
    if isinstance(node, dict):
        return any(key in node for key in ("$ref", "$dynamicRef", "$recursiveRef")) or any(
            _has_reference(value) for value in node.values()
        )
    if isinstance(node, list):
        return any(_has_reference(value) for value in node)
    return False


def validate_data(data, schema, stage):
    from .record_validation import json_value

    json_value(data)
    if set(data) != set(schema["properties"]):
        raise SealError("field_" + stage + "_schema_violation")
    try:
        schema_validator(schema).validate(data)
    except jsonschema.ValidationError:
        raise SealError("field_" + stage + "_schema_violation") from None
    except jsonschema.exceptions._WrappedReferencingError:
        raise SealError("field_schema_reference_invalid") from None


def apply_rule(value, rule, functions):
    """Only declared field-value failures become null; configuration/other faults propagate."""
    try:
        return functions[rule["entrypoint"]](deepcopy(value), **deepcopy(rule.get("params", {})))
    except (ValueError, TypeError, OverflowError):
        return None


def map_fields(output, contract, evidence, functions):
    extracted = output["data"]
    data, locators, mapped_evidence = {}, {}, {}
    for source, value in extracted.items():
        target = contract["field_mapping"].get(source, source)
        rule = contract["field_transforms"].get(source)
        transformed = apply_rule(value, rule, functions) if rule and value is not None else value
        data[target] = transformed
        if source in evidence:
            entry = {**evidence[source], "source_field": source, "target_field": target}
            if rule:
                entry["transform"] = rule
            mapped_evidence[target] = entry
            locators[target] = output["locators"][source]
    validate_data(data, contract["data_schema"], "data")
    output.update(
        data=data,
        locators=locators,
        extracted_data=extracted,
        extraction_schema_id=contract["extraction_schema"]["$id"],
        field_evidence=mapped_evidence,
    )
    return output
