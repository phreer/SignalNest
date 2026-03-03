"""
Minimal JSON-schema-like validation for tool arguments.

This module intentionally supports a small subset used by SignalNest tools:
  - type: object/string/integer/number/boolean/array
  - required
  - enum
  - minimum / maximum
  - minItems / maxItems
  - additionalProperties
  - default
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ToolSchemaError(ValueError):
    """Raised when tool arguments violate schema constraints."""


@dataclass(frozen=True)
class ValidationContext:
    tool_name: str


def _fail(ctx: ValidationContext, message: str) -> None:
    raise ToolSchemaError(f"{ctx.tool_name}: {message}")


def _check_enum(ctx: ValidationContext, value: Any, schema: dict, field: str) -> None:
    enum_values = schema.get("enum")
    if enum_values is not None and value not in enum_values:
        _fail(ctx, f"field '{field}' must be one of {enum_values}, got {value!r}")


def _validate_scalar(ctx: ValidationContext, value: Any, schema: dict, field: str) -> Any:
    expected_type = schema.get("type")
    if expected_type == "string":
        if not isinstance(value, str):
            _fail(ctx, f"field '{field}' must be string, got {type(value).__name__}")
    elif expected_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            _fail(ctx, f"field '{field}' must be integer, got {type(value).__name__}")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            _fail(ctx, f"field '{field}' must be >= {minimum}, got {value}")
        if maximum is not None and value > maximum:
            _fail(ctx, f"field '{field}' must be <= {maximum}, got {value}")
    elif expected_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            _fail(ctx, f"field '{field}' must be number, got {type(value).__name__}")
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            _fail(ctx, f"field '{field}' must be boolean, got {type(value).__name__}")
    else:
        _fail(ctx, f"field '{field}' unsupported scalar type: {expected_type!r}")

    _check_enum(ctx, value, schema, field)
    return value


def _validate_array(ctx: ValidationContext, value: Any, schema: dict, field: str) -> list:
    if not isinstance(value, list):
        _fail(ctx, f"field '{field}' must be array, got {type(value).__name__}")
    min_items = schema.get("minItems")
    max_items = schema.get("maxItems")
    if min_items is not None and len(value) < min_items:
        _fail(ctx, f"field '{field}' must contain at least {min_items} items")
    if max_items is not None and len(value) > max_items:
        _fail(ctx, f"field '{field}' must contain at most {max_items} items")

    item_schema = schema.get("items")
    if item_schema:
        validated: list = []
        for idx, item in enumerate(value):
            validated.append(_validate_value(ctx, item, item_schema, f"{field}[{idx}]"))
        value = validated
    _check_enum(ctx, value, schema, field)
    return value


def _validate_object(
    ctx: ValidationContext,
    value: Any,
    schema: dict,
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(ctx, f"field '{field}' must be object, got {type(value).__name__}")
    return value


def _validate_value(ctx: ValidationContext, value: Any, schema: dict, field: str) -> Any:
    expected_type = schema.get("type")
    if expected_type == "array":
        return _validate_array(ctx, value, schema, field)
    if expected_type == "object":
        return _validate_object(ctx, value, schema, field)
    return _validate_scalar(ctx, value, schema, field)


def validate_tool_args(tool_name: str, schema: dict, args: dict[str, Any] | None) -> dict[str, Any]:
    """
    Validate and normalize args based on the given schema.
    """
    ctx = ValidationContext(tool_name=tool_name)
    args = args or {}
    if not isinstance(args, dict):
        _fail(ctx, f"arguments must be object, got {type(args).__name__}")

    if schema.get("type") != "object":
        _fail(ctx, "root schema must be object")

    properties: dict[str, dict] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])
    additional_properties = schema.get("additionalProperties", False)

    if not additional_properties:
        unknown_fields = [k for k in args if k not in properties]
        if unknown_fields:
            _fail(ctx, f"unknown fields: {unknown_fields}")

    normalized: dict[str, Any] = {}
    for field, field_schema in properties.items():
        if field in args:
            raw_value = args[field]
        elif "default" in field_schema:
            raw_value = field_schema["default"]
        else:
            continue
        normalized[field] = _validate_value(ctx, raw_value, field_schema, field)

    for field in required:
        if field not in normalized:
            _fail(ctx, f"missing required field '{field}'")

    return normalized

