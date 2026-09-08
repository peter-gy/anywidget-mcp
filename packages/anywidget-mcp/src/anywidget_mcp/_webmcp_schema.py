"""Describe synchronized traits as WebMCP inputs."""

from __future__ import annotations

import math
from typing import Any

import traitlets as t
from anywidget import AnyWidget


def public_traits(widget: AnyWidget) -> dict[str, t.TraitType]:
    return {
        name: trait
        for name, trait in widget.traits(sync=True).items()
        if not name.startswith("_")
        and name not in {"layout", "tabbable", "tooltip"}
        and trait.metadata.get("webmcp") is not False
    }


def trait_schema(trait: t.TraitType) -> dict[str, Any] | None:
    if any(trait.metadata.get(key) is not None for key in ("to_json", "from_json")):
        return None
    schema: dict[str, Any]
    if isinstance(trait, t.Enum):
        values = trait.values
        if values is None or not all(
            value is None
            or isinstance(value, (str, bool, int))
            or (isinstance(value, float) and math.isfinite(value))
            for value in values
        ):
            return None
        schema = {"enum": list(values)}
    elif isinstance(trait, t.Bool):
        schema = {"type": "boolean"}
    elif isinstance(trait, (t.Int, t.Float)):
        schema = {"type": "integer" if isinstance(trait, t.Int) else "number"}
        for attribute, key in (("min", "minimum"), ("max", "maximum")):
            bound = getattr(trait, attribute, None)
            if isinstance(bound, (int, float)) and math.isfinite(bound):
                schema[key] = bound
    elif isinstance(trait, t.Unicode):
        schema = {"type": "string"}
    elif isinstance(trait, t.List):
        item = trait_schema(trait._trait) if trait._trait is not None else {}
        if item is None:
            return None
        schema = {"type": "array", "items": item}
        if trait._minlen:
            schema["minItems"] = trait._minlen
        if trait._maxlen < (1 << 53) - 1:
            schema["maxItems"] = trait._maxlen
    elif isinstance(trait, t.Dict):
        value_trait = trait._value_trait
        key_trait = trait._key_trait
        value = (
            trait_schema(value_trait) if isinstance(value_trait, t.TraitType) else {}
        )
        if value is None or (
            key_trait is not None and not isinstance(key_trait, t.Unicode)
        ):
            return None
        schema = {"type": "object", "additionalProperties": value}
    elif isinstance(trait, t.Any):
        schema = {}
    elif isinstance(trait, t.Union):
        variants = [trait_schema(option) for option in trait.trait_types]
        if any(option is None for option in variants):
            return None
        schema = {"anyOf": variants}
    else:
        return None

    if trait.allow_none:
        schema = {"anyOf": [schema, {"type": "null"}]}
    if trait.help:
        schema["description"] = trait.help
    return schema
