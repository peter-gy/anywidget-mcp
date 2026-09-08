"""Compile source facts through explicit inspection and input-schema ports."""

from typing import TypeVar, overload

from .inputs import json_inputs
from .ports import InputCompiler, ModelBinding, OwnedSession, SourceInspector
from .source import classify, describe_model, identity, inspect_source, rename
from .types import (
    CallableSpec,
    Identity,
    InputSpec,
    JsonSchema,
    ModelCapabilities,
    ModelSpec,
    ResultSpec,
    TargetCapabilities,
    TargetKind,
    TargetSpec,
    TraitSpec,
    ValueReference,
)


def scan(source: object, *, inspector: SourceInspector = inspect_source) -> TargetSpec:
    return inspector(source)


InputsT = TypeVar("InputsT", bound=InputSpec)


@overload
def compile_inputs(spec: TargetSpec) -> InputSpec: ...


@overload
def compile_inputs(spec: TargetSpec, compiler: InputCompiler[InputsT]) -> InputsT: ...


def compile_inputs(
    spec: TargetSpec, compiler: InputCompiler[InputSpec] = json_inputs
) -> InputSpec:
    if spec.call is None:
        raise TypeError("Input compilation requires a widget class or callable factory")
    return compiler(spec.call)


__all__ = [
    "CallableSpec",
    "Identity",
    "InputCompiler",
    "InputSpec",
    "JsonSchema",
    "ModelBinding",
    "ModelCapabilities",
    "ModelSpec",
    "OwnedSession",
    "ResultSpec",
    "SourceInspector",
    "TargetCapabilities",
    "TargetKind",
    "TargetSpec",
    "TraitSpec",
    "ValueReference",
    "classify",
    "compile_inputs",
    "describe_model",
    "identity",
    "json_inputs",
    "rename",
    "scan",
]
