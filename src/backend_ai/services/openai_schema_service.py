"""Native structured-output request checks and independent final validation."""

import json
from dataclasses import dataclass
from itertools import islice

from jsonschema import Draft202012Validator, SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from src.backend_ai.routers.openai_errors import OpenAIAPIError


@dataclass(frozen=True)
class SchemaValidationResult:
    valid: bool
    errors: list[str]


def validate_schema_request(schema, *, param="response_format", strict=False):
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise OpenAIAPIError(400, "Invalid JSON Schema.", param=param, code="invalid_request") from exc
    unsupported = {"patternProperties", "not", "if", "then", "else", "unevaluatedProperties", "dependentSchemas", "dependentRequired", "allOf", "$dynamicRef", "$recursiveRef"}
    if strict and (not isinstance(schema, dict) or schema.get("type") != "object" or "anyOf" in schema):
        raise OpenAIAPIError(400, "Strict schemas require an object at the root.", param=param, code="invalid_request")

    def check(node, depth=0):
        if depth > 32:
            raise OpenAIAPIError(400, "Schema nesting exceeds this profile's limit.", param=param, code="unsupported_parameter")
        if isinstance(node, dict):
            if unsupported.intersection(node) or ("$ref" in node and not str(node["$ref"]).startswith("#/")):
                raise OpenAIAPIError(400, "This schema feature is not supported.", param=param, code="unsupported_parameter")
            if strict and (node.get("type") == "object" or "properties" in node):
                if node.get("additionalProperties") is not False or set(node.get("required", [])) != set(node.get("properties", {})):
                    raise OpenAIAPIError(400, "Strict objects require every property and additionalProperties false.",
                                         param=param, code="invalid_request")
            for key in ("properties", "$defs", "definitions"):
                for value in node.get(key, {}).values():
                    check(value, depth + 1)
            for key in ("items", "additionalProperties", "contains", "propertyNames"):
                if key in node:
                    check(node[key], depth + 1)
            for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
                for value in node.get(key, []):
                    check(value, depth + 1)

    check(schema)


def validate_output_schema(output: str, schema: dict) -> SchemaValidationResult:
    try:
        parsed = json.loads(output, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (TypeError, ValueError):
        return SchemaValidationResult(False, ["invalid_json"])
    try:
        # Empty registry has no retrieval callback. A caller schema can only
        # reference resources embedded in that schema, never network/files.
        validator = Draft202012Validator(schema, registry=Registry())
        errors = ['schema_violation' for _ in islice(validator.iter_errors(parsed), 10)]
    except (Unresolvable, RecursionError, SchemaError, ValueError, TypeError):
        return SchemaValidationResult(False, ['invalid_schema_reference'])
    return SchemaValidationResult(not errors, errors[:10])
