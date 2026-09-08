"""Compile resolved parameters into JSON schemas and Python argument validators."""

from __future__ import annotations

import inspect
import json
import math
import types
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from .types import CallableSpec, InputSpec, JsonSchema


@dataclass(frozen=True)
class _ValueType:
    schema: dict[str, Any]
    validate: Callable[[Any, str], Any]


def json_inputs(definition: CallableSpec) -> InputSpec:
    """Compile finite JSON inputs and preserve Python defaults and enum members."""
    parameters: dict[str, _ValueType] = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in definition.parameters:
        annotation = parameter.annotation
        if annotation is inspect.Parameter.empty:
            raise TypeError(f"Parameter {parameter.name!r} requires a type annotation")
        value_type = _compile_type(annotation)
        parameters[parameter.name] = value_type
        properties[parameter.name] = dict(value_type.schema)
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter.name)
        else:
            try:
                _compile_type(annotation, python_defaults=True).validate(
                    parameter.default, parameter.name
                )
                default = json.loads(
                    json.dumps(parameter.default, default=_enum_value, allow_nan=False)
                )
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"Invalid default for {parameter.name!r}: {error}"
                ) from error
            properties[parameter.name]["default"] = default

    def validate(arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        unknown = arguments.keys() - parameters.keys()
        if unknown:
            raise ValueError(f"Unknown tool arguments: {', '.join(map(str, unknown))}")
        missing = set(required) - arguments.keys()
        if missing:
            raise ValueError(f"Missing tool arguments: {', '.join(sorted(missing))}")
        return {
            name: parameters[name].validate(value, name)
            for name, value in arguments.items()
        }

    return InputSpec(
        definition.signature,
        JsonSchema(
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
        ),
        validate,
    )


def _compile_type(annotation: Any, *, python_defaults: bool = False) -> _ValueType:
    if annotation is Any:
        return _ValueType({}, _validate_json)
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return _compile_type(arguments[0], python_defaults=python_defaults)
    if origin in (Union, types.UnionType):
        choices = [
            _compile_type(choice, python_defaults=python_defaults)
            for choice in arguments
        ]

        def validate_union(value: Any, path: str) -> Any:
            for choice in choices:
                try:
                    return choice.validate(value, path)
                except ValueError:
                    pass
            raise ValueError(f"{path} must match one of its declared types")

        return _ValueType(
            {"anyOf": [choice.schema for choice in choices]}, validate_union
        )
    if origin is Literal or (
        inspect.isclass(annotation) and issubclass(annotation, Enum)
    ):
        values = list(arguments) if origin is Literal else list(annotation)
        encoded = [
            _enum_value(value) if isinstance(value, Enum) else value for value in values
        ]
        for value in encoded:
            if type(value) not in (str, int, float, bool, type(None)) or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                raise TypeError("Literal and Enum values must be finite JSON scalars")

        def validate_enum(value: Any, path: str) -> Any:
            candidates = values if python_defaults else encoded
            for candidate, member in zip(candidates, values):
                if type(value) is type(candidate) and value == candidate:
                    return member
            raise ValueError(f"{path} must be one of {encoded!r}")

        return _ValueType({"enum": encoded}, validate_enum)
    if origin is list and len(arguments) == 1:
        item_type = _compile_type(arguments[0], python_defaults=python_defaults)

        def validate_list(value: Any, path: str) -> list[Any]:
            if not isinstance(value, list):
                raise ValueError(f"{path} must be an array")
            return [
                item_type.validate(item, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]

        return _ValueType({"type": "array", "items": item_type.schema}, validate_list)
    if origin is dict and len(arguments) == 2 and arguments[0] is str:
        item_type = _compile_type(arguments[1], python_defaults=python_defaults)

        def validate_dict(value: Any, path: str) -> dict[str, Any]:
            if not isinstance(value, dict) or any(
                not isinstance(key, str) for key in value
            ):
                raise ValueError(f"{path} must be an object with string keys")
            return {
                key: item_type.validate(item, f"{path}.{key}")
                for key, item in value.items()
            }

        return _ValueType(
            {"type": "object", "additionalProperties": item_type.schema}, validate_dict
        )
    primitive = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        type(None): "null",
    }
    if annotation not in primitive:
        raise TypeError(f"Unsupported JSON parameter type {annotation!r}")
    kind = primitive[annotation]

    def validate_scalar(value: Any, path: str) -> Any:
        if annotation is float:
            valid = type(value) in (int, float) and (
                not isinstance(value, float) or math.isfinite(value)
            )
        else:
            valid = type(value) is annotation
        if not valid:
            raise ValueError(f"{path} must be {kind}")
        return value

    return _ValueType({"type": kind}, validate_scalar)


def _validate_json(value: Any, path: str) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [
            _validate_json(item, f"{path}[{index}]") for index, item in enumerate(value)
        ]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {
            key: _validate_json(item, f"{path}.{key}") for key, item in value.items()
        }
    raise ValueError(f"{path} must contain finite JSON values")


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"{type(value).__name__} is not JSON serializable")
