"""Resolve instance-local WebMCP exposure settings."""

from __future__ import annotations

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass, replace
from typing import Any, TypedDict

from .._spec import ModelSpec, TraitSpec


class WidgetOptions(TypedDict, total=False):
    name: str
    title: str
    description: str
    traits: Set[str] | None
    private: Set[str]
    read_only: bool | Set[str]


@dataclass(frozen=True)
class Exposure:
    name: str | None = None
    title: str | None = None
    description: str | None = None
    traits: frozenset[str] | None = None
    private: frozenset[str] = frozenset()
    read_only: bool | frozenset[str] = False

    def patch(self, values: Mapping[str, Any]) -> Exposure:
        fields: dict[str, Any] = {}
        for key, value in values.items():
            if key in {"name", "title", "description"}:
                if not isinstance(value, str):
                    raise TypeError(f"WebMCP {key} must be a string")
                if key == "name":
                    validate_name(value)
            elif key in {"traits", "private", "read_only"}:
                if (key == "traits" and value is None) or (
                    key == "read_only" and isinstance(value, bool)
                ):
                    pass
                elif isinstance(value, Set) and all(
                    isinstance(name, str) for name in value
                ):
                    value = frozenset(value)
                else:
                    raise TypeError(f"WebMCP {key} must be a set of trait names")
            else:
                raise TypeError(f"Unknown WebMCP option: {key!r}")
            fields[key] = value
        return replace(self, **fields)

    def validate(self, model: ModelSpec) -> None:
        names = set(self.traits or ()) | self.private
        if isinstance(self.read_only, frozenset):
            names |= self.read_only
        unknown = names - set(model.capabilities.read)
        if unknown:
            raise ValueError(
                f"Unknown WebMCP traits for {model.identity.python_name}: {', '.join(sorted(unknown))}"
            )

    def writable(self, name: str) -> bool:
        return self.read_only is not True and (
            self.read_only is False or name not in self.read_only
        )

    def select(self, model: ModelSpec) -> tuple[TraitSpec, ...]:
        return tuple(
            trait
            for trait in model.traits
            if trait.default_visible
            and trait.metadata.get("webmcp") is not False
            and trait.name not in self.private
            and (self.traits is None or trait.name in self.traits)
        )

    def writable_traits(self, model: ModelSpec) -> tuple[TraitSpec, ...]:
        return tuple(
            trait
            for trait in self.select(model)
            if trait.name in model.capabilities.write and self.writable(trait.name)
        )


def validate_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise ValueError("WebMCP name must contain 1–64 letters, digits, _, . or -")
