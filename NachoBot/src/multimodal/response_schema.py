"""Dependency-free, bounded response-schema validation for image perception.

This intentionally implements a small subset of JSON Schema rather than
resolving references or supporting remote validators. Accepted schemas and
model output are copied/parsed into bounded JSON values before validation.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable

from src.llm_models.exceptions import ModelOutputValidationError
from .contracts import MAX_TEXT_CHARS


MAX_RESPONSE_SCHEMA_BYTES = 16 * 1024
MAX_RESPONSE_SCHEMA_DEPTH = 8
MAX_RESPONSE_SCHEMA_NODES = 128
MAX_RESPONSE_OUTPUT_DEPTH = 32
MAX_RESPONSE_OUTPUT_NODES = 4096
_SCHEMA_ERROR = "invalid response_schema"
_SUPPORTED_TYPES = frozenset({"object", "string", "number", "integer", "boolean", "null"})
_SUPPORTED_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "minimum", "maximum", "minLength", "maxLength"}
)


def _schema_error() -> ValueError:
    return ValueError(_SCHEMA_ERROR)


def _check_json_tree(value: Any, *, max_depth: int, max_nodes: int) -> None:
    """Reject non-JSON input and bound its shape before serialization/parsing."""

    stack = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise _schema_error()
        if current is None or type(current) in (str, bool, int):
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise _schema_error()
            continue
        if type(current) is dict:
            for key, child in current.items():
                if type(key) is not str:
                    raise _schema_error()
                stack.append((child, depth + 1))
            continue
        if type(current) is list:
            stack.extend((child, depth + 1) for child in current)
            continue
        raise _schema_error()


def _canonical_schema(schema: Any) -> dict[str, Any]:
    if type(schema) is not dict:
        raise _schema_error()
    _check_json_tree(
        schema,
        max_depth=MAX_RESPONSE_SCHEMA_DEPTH,
        max_nodes=MAX_RESPONSE_SCHEMA_NODES,
    )
    try:
        encoded = json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_SCHEMA_BYTES:
            raise _schema_error()
        # Parsing the canonical representation also detaches validation from a
        # caller-owned mutable dict before any provider await can occur.
        copied = json.loads(encoded)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        if isinstance(exc, ValueError) and str(exc) == _SCHEMA_ERROR:
            raise
        raise _schema_error() from exc
    return copied


def _validate_schema_node(schema: Any, *, root: bool = False) -> dict[str, Any]:
    if type(schema) is not dict or not schema or any(key not in _SUPPORTED_KEYWORDS for key in schema):
        raise _schema_error()
    schema_type = schema.get("type")
    if type(schema_type) is not str or schema_type not in _SUPPORTED_TYPES:
        raise _schema_error()
    if root and schema_type != "object":
        raise _schema_error()

    object_keywords = {"properties", "required", "additionalProperties"}
    numeric_keywords = {"minimum", "maximum"}
    string_keywords = {"minLength", "maxLength"}
    applicable = {
        "object": object_keywords,
        "number": numeric_keywords,
        "integer": numeric_keywords,
        "string": string_keywords,
    }.get(schema_type, set())
    if set(schema) - {"type"} - applicable:
        raise _schema_error()

    if schema_type == "object":
        properties = schema.get("properties", {})
        if type(properties) is not dict:
            raise _schema_error()
        normalized_properties = {}
        for name, child_schema in properties.items():
            if type(name) is not str:
                raise _schema_error()
            normalized_properties[name] = _validate_schema_node(child_schema)

        required = schema.get("required", [])
        if type(required) is not list or any(type(name) is not str for name in required):
            raise _schema_error()
        if len(set(required)) != len(required) or any(name not in properties for name in required):
            raise _schema_error()

        additional = schema.get("additionalProperties", True)
        if type(additional) is not bool:
            raise _schema_error()
        return {
            "type": schema_type,
            "properties": normalized_properties,
            "required": frozenset(required),
            "additionalProperties": additional,
        }

    if schema_type in {"number", "integer"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        for key, value in (("minimum", minimum), ("maximum", maximum)):
            if key in schema and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or isinstance(value, float) and not math.isfinite(value)
            ):
                raise _schema_error()
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _schema_error()
        return {"type": schema_type, "minimum": minimum, "maximum": maximum}

    if schema_type == "string":
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        for key, value in (("minLength", minimum), ("maxLength", maximum)):
            if key in schema and (type(value) is not int or value < 0):
                raise _schema_error()
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _schema_error()
        return {"type": schema_type, "minLength": minimum, "maxLength": maximum}

    return {"type": schema_type}


def _strict_json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _strict_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _bounded_output_tree(value: Any) -> bool:
    stack = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_RESPONSE_OUTPUT_DEPTH or nodes > MAX_RESPONSE_OUTPUT_NODES:
            return False
        if type(current) is dict:
            stack.extend((child, depth + 1) for child in current.values())
        elif type(current) is list:
            stack.extend((child, depth + 1) for child in current)
    return True


def _matches(value: Any, schema: dict[str, Any]) -> bool:
    schema_type = schema["type"]
    if schema_type == "object":
        if type(value) is not dict:
            return False
        properties = schema["properties"]
        if not schema["required"].issubset(value):
            return False
        if not schema["additionalProperties"] and any(key not in properties for key in value):
            return False
        return all(
            key not in properties or _matches(child, properties[key])
            for key, child in value.items()
        )
    if schema_type == "string":
        return (
            type(value) is str
            and (schema["minLength"] is None or len(value) >= schema["minLength"])
            and (schema["maxLength"] is None or len(value) <= schema["maxLength"])
        )
    if schema_type == "boolean":
        return type(value) is bool
    if schema_type == "null":
        return value is None
    if schema_type == "integer":
        is_integer = type(value) is int or type(value) is float and value.is_integer()
        if not is_integer:
            return False
    elif schema_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
    else:
        return False

    if isinstance(value, float) and not math.isfinite(value):
        return False
    return (schema["minimum"] is None or value >= schema["minimum"]) and (
        schema["maximum"] is None or value <= schema["maximum"]
    )


def build_response_validator(response_schema: Any) -> Callable[[str], None]:
    """Validate a restricted object schema and return a strict output callback.

    Supported keywords are ``type``, ``properties``, ``required``,
    ``additionalProperties`` (boolean), ``minimum``, ``maximum``,
    ``minLength`` and ``maxLength``. Every schema node declares one supported
    type; the root must be an object. References, arrays, regexes, formats,
    coercion, remote lookups, and unknown keywords are intentionally rejected.
    """

    schema = _validate_schema_node(_canonical_schema(response_schema), root=True)

    def validate(content: str) -> None:
        if type(content) is not str or len(content) > MAX_TEXT_CHARS:
            raise ModelOutputValidationError()
        try:
            value = json.loads(
                content,
                object_pairs_hook=_strict_json_object_pairs,
                parse_float=_strict_json_float,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ModelOutputValidationError() from exc
        if not _bounded_output_tree(value) or not _matches(value, schema):
            raise ModelOutputValidationError()

    return validate
