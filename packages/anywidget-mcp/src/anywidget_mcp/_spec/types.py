"""Transport-independent descriptions of widget targets and model capabilities."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

TargetKind = Literal["instance", "widget-class", "factory"]
Schema = dict[str, Any]


@dataclass(frozen=True, init=False)
class JsonSchema:
    """Store a JSON schema and export a fresh dictionary for each consumer."""

    json: str

    def __init__(self, value: Mapping[str, Any]) -> None:
        object.__setattr__(
            self,
            "json",
            json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def to_dict(self) -> Schema:
        return json.loads(self.json)


@dataclass(frozen=True)
class ValueReference:
    """Identify an ancestor container by child indexes within a value snapshot."""

    path: tuple[int, ...]


def _freeze(
    value: Any,
    ancestors: dict[int, tuple[int, ...]] | None = None,
    path: tuple[int, ...] = (),
) -> Any:
    if not isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return value
    if ancestors is None:
        ancestors = {}
    if id(value) in ancestors:
        return ValueReference(ancestors[id(value)])
    ancestors[id(value)] = path
    try:
        if isinstance(value, Mapping):
            return MappingProxyType(
                {
                    key: _freeze(item, ancestors, (*path, index))
                    for index, (key, item) in enumerate(value.items())
                }
            )
        items = (
            _freeze(item, ancestors, (*path, index)) for index, item in enumerate(value)
        )
        return frozenset(items) if isinstance(value, (set, frozenset)) else tuple(items)
    finally:
        del ancestors[id(value)]


@dataclass(frozen=True)
class Identity:
    python_name: str
    name: str
    title: str
    description: str


@dataclass(frozen=True)
class ResultSpec:
    annotation: Any = inspect.Signature.empty
    widget_type: type | None = None
    cardinality: Literal["single", "sequence", "unknown"] = "unknown"
    managed: bool | None = None


@dataclass(frozen=True)
class CallableSpec:
    invoke: Callable[..., Any] = field(repr=False, compare=False)
    signature: inspect.Signature
    result: ResultSpec
    asynchronous: bool

    @property
    def parameters(self) -> tuple[inspect.Parameter, ...]:
        return tuple(self.signature.parameters.values())


@dataclass(frozen=True)
class TraitSpec:
    name: str
    type_name: str
    description: str
    synchronized: bool
    read_only: bool
    allow_none: bool
    default: Any
    metadata: Mapping[str, Any]
    input_schema: JsonSchema | None
    default_visible: bool
    write_restriction: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "default", _freeze(self.default))


@dataclass(frozen=True)
class ModelCapabilities:
    read: tuple[str, ...]
    write: tuple[str, ...]
    observe: tuple[str, ...]


@dataclass(frozen=True)
class ModelSpec:
    identity: Identity
    traits: tuple[TraitSpec, ...]
    capabilities: ModelCapabilities

    @property
    def synchronized_names(self) -> tuple[str, ...]:
        return tuple(trait.name for trait in self.traits if trait.synchronized)

    @property
    def default_state_names(self) -> tuple[str, ...]:
        return tuple(trait.name for trait in self.traits if trait.default_visible)

    def trait(self, name: str) -> TraitSpec:
        for trait in self.traits:
            if trait.name == name:
                return trait
        raise KeyError(name)


@dataclass(frozen=True)
class TargetCapabilities:
    create: bool
    read: bool
    update: bool
    observe: bool


@dataclass(frozen=True)
class TargetSpec:
    source: Any = field(repr=False, compare=False)
    kind: TargetKind
    identity: Identity
    call: CallableSpec | None
    model: ModelSpec | None
    capabilities: TargetCapabilities


@dataclass(frozen=True)
class InputSpec:
    signature: inspect.Signature
    schema: JsonSchema
    validate: Callable[[dict[str, Any]], dict[str, Any]] = field(
        repr=False, compare=False
    )
