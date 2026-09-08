from __future__ import annotations

from enum import Enum
from functools import wraps
from types import FunctionType
from typing import Annotated, Any, Literal

from anywidget import AnyWidget
import pytest

from anywidget_mcp._spec import compile_inputs, scan


class Region(Enum):
    EU = "EU"
    US = "US"


class Counter(AnyWidget):
    _esm = "export default { render() {} };"


def create_counter(start: int = 0, *, label: str, enabled: bool = True) -> Counter:
    """Create a labeled counter."""
    return Counter()


def test_named_parameters_define_creation_schema_and_validate_arguments() -> None:
    tool = compile_inputs(scan(create_counter))
    assert tool.schema.to_dict() == {
        "type": "object",
        "properties": {
            "start": {"type": "integer", "default": 0},
            "label": {"type": "string"},
            "enabled": {"type": "boolean", "default": True},
        },
        "required": ["label"],
        "additionalProperties": False,
    }
    assert tool.validate({"label": "Sales"}) == {"label": "Sales"}
    assert tool.validate({"start": 3, "label": "Sales", "enabled": False}) == {
        "start": 3,
        "label": "Sales",
        "enabled": False,
    }


@pytest.mark.parametrize(
    "arguments, error",
    [
        ({}, "Missing tool arguments: label"),
        ({"label": "Sales", "extra": 2}, "Unknown tool arguments: extra"),
        ({"label": "Sales", "start": True}, "start must be integer"),
        ({"label": "Sales", "start": "3"}, "start must be integer"),
        ({"label": "Sales", "enabled": 1}, "enabled must be boolean"),
        ([], "Tool arguments must be an object"),
    ],
)
def test_invalid_creation_arguments_are_rejected(arguments: Any, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        compile_inputs(scan(create_counter)).validate(arguments)


def test_nested_arguments_preserve_json_shapes_and_convert_enum_members() -> None:
    def create_plot(
        region: Region = Region.EU,
        rows: list[dict[str, float | None]] = [],
        mode: Literal["points", "lines"] = "points",
        label: Annotated[str | None, "Display label"] = None,
    ) -> Counter:
        return Counter()

    tool = compile_inputs(scan(create_plot))
    assert tool.schema.to_dict()["properties"]["region"] == {
        "enum": ["EU", "US"],
        "default": "EU",
    }
    assert tool.schema.to_dict()["properties"]["rows"]["items"] == {
        "type": "object",
        "additionalProperties": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    }
    assert tool.validate(
        {"region": "US", "rows": [{"x": 2, "y": None}], "mode": "lines", "label": None}
    ) == {
        "region": Region.US,
        "rows": [{"x": 2, "y": None}],
        "mode": "lines",
        "label": None,
    }
    for arguments in (
        {"rows": [{"x": True}]},
        {"rows": [{"x": float("inf")}]},
        {"rows": [{1: 2}]},
        {"region": "UK"},
        {"mode": 1},
        {"label": 3},
    ):
        with pytest.raises(ValueError):
            tool.validate(arguments)


def test_classes_expose_named_constructor_parameters_or_no_argument_creation() -> None:
    assert compile_inputs(scan(Counter)).schema.to_dict() == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    class NamedCounter(Counter):
        def __init__(self, start: int = 0, **kwargs: Any) -> None:
            super().__init__(**kwargs)

    tool = compile_inputs(scan(NamedCounter))
    assert tool.schema.to_dict()["properties"] == {
        "start": {"type": "integer", "default": 0}
    }


def test_variadic_positional_and_non_json_signatures_fail_at_registration() -> None:
    def variadic(**kwargs: int):
        return Counter()

    def positional(start: int, /):
        return Counter()

    def incompatible(data: bytes):
        return Counter()

    for target, error in (
        (variadic, "explicit names"),
        (positional, "keyword arguments"),
        (incompatible, "Unsupported JSON parameter type"),
    ):
        with pytest.raises(TypeError, match=error):
            compile_inputs(scan(target))


@pytest.mark.parametrize(
    "annotation, default",
    [(int, "zero"), (list[int], (1, 2)), (dict[str, int], {1: 2}), (Region, "EU")],
)
def test_defaults_incompatible_with_declared_python_types_fail_at_registration(
    annotation: Any, default: Any
) -> None:
    def create_with_default(value=default):
        return Counter()

    create_with_default.__annotations__["value"] = annotation
    with pytest.raises(ValueError, match="Invalid default for 'value'"):
        compile_inputs(scan(create_with_default))


def test_any_arguments_validate_nested_finite_json() -> None:
    def create_payload(payload: Any, metadata: dict[str, Any]) -> Counter:
        return Counter()

    tool = compile_inputs(scan(create_payload))
    assert tool.schema.to_dict()["properties"] == {
        "payload": {},
        "metadata": {"type": "object", "additionalProperties": {}},
    }
    arguments = {
        "payload": [{"enabled": True, "value": None}, 2, 3.5],
        "metadata": {"label": "Sales"},
    }
    assert tool.validate(arguments) == arguments
    for invalid in ([float("nan")], {"x": float("inf")}, {1: "value"}, (1, 2), b"data"):
        with pytest.raises(ValueError, match="finite JSON values"):
            tool.validate({"payload": invalid, "metadata": {}})


def test_wrapped_creation_annotations_resolve_in_the_factory_namespace() -> None:
    def create_region(region: Region) -> Counter:
        return Counter()

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return create_region(*args, **kwargs)

    foreign_wrapper = wraps(create_region)(
        FunctionType(wrapper.__code__, {}, closure=wrapper.__closure__)
    )
    tool = compile_inputs(scan(foreign_wrapper))
    assert tool.validate({"region": "EU"}) == {"region": Region.EU}
