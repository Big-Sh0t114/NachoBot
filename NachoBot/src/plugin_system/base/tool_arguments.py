"""Pure, immutable tool-definition snapshots and argument validation.

This module is intentionally safe to import without starting the application.
It is shared by host dispatch and ``BaseTool.direct_execute`` so both paths
enforce the same tuple-defined contract.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.llm_models.payload_content.tool_option import ToolParamType


class ToolDefinitionError(ValueError):
    """A tool's declared contract is malformed."""

    def __init__(self, code: str, parameter_name: str | None = None) -> None:
        self.code = code
        self.parameter_name = parameter_name
        suffix = f": {parameter_name}" if parameter_name is not None else ""
        super().__init__(f"invalid_definition: {code}{suffix}")


class ToolArgumentError(ValueError):
    """A call's arguments do not satisfy its pinned tool contract."""

    def __init__(self, code: str, parameter_name: str | None = None) -> None:
        self.code = code
        self.parameter_name = parameter_name
        suffix = f": {parameter_name}" if parameter_name is not None else ""
        super().__init__(f"invalid_arguments: {code}{suffix}")


@dataclass(frozen=True, slots=True)
class ToolParameterSpec:
    name: str
    param_type: ToolParamType
    description: str
    required: bool
    enum_values: tuple[str | int | float | bool, ...] | None


@dataclass(frozen=True, slots=True)
class ToolDefinitionContract:
    """An immutable contract plus a token for the provider-visible metadata."""

    name: str
    description: str
    parameters: tuple[ToolParameterSpec, ...]
    token: str
    input_schema_json: str | None = None

    def render_provider_definition(self) -> dict[str, Any]:
        """Render detached mutable provider data from this immutable snapshot."""
        rendered_parameters = [
            (
                parameter.name,
                parameter.param_type,
                parameter.description,
                parameter.required,
                list(parameter.enum_values) if parameter.enum_values is not None else None,
            )
            for parameter in self.parameters
        ]
        definition: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": rendered_parameters,
        }
        if self.input_schema_json is not None:
            definition["input_schema"] = json.loads(self.input_schema_json)
        return definition


def _definition_error(code: str, parameter_name: Any = None) -> ToolDefinitionError:
    safe_name = parameter_name if type(parameter_name) is str and parameter_name else None
    return ToolDefinitionError(code, safe_name)


def _argument_error(code: str, parameter_name: str | None = None) -> ToolArgumentError:
    return ToolArgumentError(code, parameter_name)


def _is_supported_scalar(value: Any) -> bool:
    if type(value) in (str, int, bool):
        return True
    return type(value) is float and math.isfinite(value)


def _matches_parameter_type(param_type: ToolParamType, value: Any) -> bool:
    if param_type is ToolParamType.STRING:
        return type(value) is str
    if param_type is ToolParamType.INTEGER:
        return type(value) is int
    if param_type is ToolParamType.FLOAT:
        return type(value) is int or (type(value) is float and math.isfinite(value))
    if param_type is ToolParamType.BOOLEAN:
        return type(value) is bool
    return False


def compile_tool_parameters(parameters: Any) -> tuple[ToolParameterSpec, ...]:
    """Compile authoritative five-tuple metadata into immutable specs."""
    if type(parameters) not in (list, tuple):
        raise _definition_error("invalid_parameters")

    compiled: list[ToolParameterSpec] = []
    names: set[str] = set()
    for item in parameters:
        if type(item) is not tuple or len(item) != 5:
            raise _definition_error("invalid_parameter_tuple")

        name, param_type, description, required, enum_values = item
        safe_name = name if type(name) is str and name else None
        if safe_name is None:
            raise _definition_error("invalid_parameter_name")
        if safe_name in names:
            raise _definition_error("duplicate_parameter", safe_name)
        names.add(safe_name)
        if type(param_type) is not ToolParamType:
            raise _definition_error("invalid_parameter_type", safe_name)
        if type(description) is not str or not description:
            raise _definition_error("invalid_parameter_description", safe_name)
        if type(required) is not bool:
            raise _definition_error("invalid_required_flag", safe_name)

        normalized_enum: tuple[str | int | float | bool, ...] | None
        if enum_values is None:
            normalized_enum = None
        elif type(enum_values) is not list:
            raise _definition_error("invalid_enum", safe_name)
        elif not enum_values:
            # Existing providers omit empty enum lists, so normalize them to None.
            normalized_enum = None
        else:
            scalar_type: type | str | None = None
            values: list[str | int | float | bool] = []
            for value in enum_values:
                if not _is_supported_scalar(value) or not _matches_parameter_type(param_type, value):
                    raise _definition_error("invalid_enum", safe_name)
                value_type: type | str = "number" if param_type is ToolParamType.FLOAT else type(value)
                if scalar_type is None:
                    scalar_type = value_type
                elif value_type != scalar_type:
                    raise _definition_error("inconsistent_enum", safe_name)
                values.append(value)
            normalized_enum = tuple(values)

        compiled.append(
            ToolParameterSpec(
                name=safe_name,
                param_type=param_type,
                description=description,
                required=required,
                enum_values=normalized_enum,
            )
        )
    return tuple(compiled)


def _ensure_json_value(value: Any) -> None:
    """Reject Python-only values before canonical stdlib JSON round-tripping."""
    if type(value) is dict:
        for key, nested in value.items():
            if type(key) is not str:
                raise ValueError("non-string JSON object key")
            _ensure_json_value(nested)
        return
    if type(value) is list:
        for nested in value:
            _ensure_json_value(nested)
        return
    if type(value) in (str, int, bool) or value is None:
        return
    if type(value) is float and math.isfinite(value):
        return
    raise ValueError("unsupported JSON value")


def _canonical_schema(value: Any) -> str:
    if type(value) is not dict:
        raise ValueError("input schema must be an object")
    _ensure_json_value(value)
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    detached = json.loads(serialized)
    if type(detached) is not dict:
        raise ValueError("input schema must be an object")
    return json.dumps(detached, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def snapshot_tool_definition(definition: Any) -> ToolDefinitionContract:
    """Validate and detach all provider-visible metadata from a definition."""
    if type(definition) is not dict:
        raise _definition_error("invalid_definition")
    name = definition.get("name")
    description = definition.get("description")
    if type(name) is not str or not name:
        raise _definition_error("invalid_tool_name")
    if type(description) is not str or not description:
        raise _definition_error("invalid_tool_description")
    if "parameters" not in definition:
        raise _definition_error("invalid_parameters")

    parameters = compile_tool_parameters(definition["parameters"])
    schema_json: str | None = None
    if "input_schema" in definition and definition["input_schema"] is not None:
        try:
            schema_json = _canonical_schema(definition["input_schema"])
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise _definition_error("invalid_input_schema") from None

    token_payload = {
        "name": name,
        "description": description,
        "parameters": [
            {
                "name": parameter.name,
                "type": parameter.param_type.value,
                "description": parameter.description,
                "required": parameter.required,
                "enum": list(parameter.enum_values) if parameter.enum_values is not None else None,
            }
            for parameter in parameters
        ],
        "input_schema": json.loads(schema_json) if schema_json is not None else None,
    }
    try:
        canonical = json.dumps(
            token_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise _definition_error("invalid_definition") from None
    return ToolDefinitionContract(
        name=name,
        description=description,
        parameters=parameters,
        token=token,
        input_schema_json=schema_json,
    )


def validate_tool_arguments(
    contract: ToolDefinitionContract,
    arguments: Any,
    *,
    injected_values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fresh argument dict after validating declared fields only.

    ``None`` is the no-argument call shape. Other roots must be mappings with
    string keys. Unknown fields are preserved for compatibility with open
    records; declared fields receive exact scalar checks without coercion.
    """
    if not isinstance(contract, ToolDefinitionContract):
        raise _argument_error("invalid_contract")
    if arguments is None:
        result: dict[str, Any] = {}
    elif not isinstance(arguments, Mapping):
        raise _argument_error("invalid_mapping")
    else:
        try:
            result = dict(arguments)
        except Exception:
            raise _argument_error("invalid_mapping") from None

    if any(type(key) is not str for key in result):
        raise _argument_error("invalid_key")

    if injected_values is not None:
        if not isinstance(injected_values, Mapping):
            raise _argument_error("invalid_injected_values")
        try:
            overrides = dict(injected_values)
        except Exception:
            raise _argument_error("invalid_injected_values") from None
        if any(type(key) is not str for key in overrides):
            raise _argument_error("invalid_injected_values")
        result.update(overrides)

    for parameter in contract.parameters:
        if parameter.name not in result:
            if parameter.required:
                raise _argument_error("missing_required", parameter.name)
            continue
        value = result[parameter.name]
        if not _matches_parameter_type(parameter.param_type, value):
            raise _argument_error("invalid_type", parameter.name)
        if parameter.enum_values is not None and value not in parameter.enum_values:
            raise _argument_error("invalid_enum", parameter.name)
    return result

