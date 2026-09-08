"""Inspect Python targets and trait declarations before runtime acquisition."""

from __future__ import annotations

import functools
import inspect
import re
import sys
import types
from collections.abc import (
    Awaitable,
    Callable,
    Coroutine,
    Generator,
    Iterator,
    AsyncGenerator,
    AsyncIterator,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import (
    Annotated,
    Any,
    Literal,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

import traitlets as t
from anywidget import AnyWidget
from anywidget._descriptor import MimeBundleDescriptor, ReprMimeBundle

from .traits import trait_schema
from .types import (
    CallableSpec,
    Identity,
    JsonSchema,
    ModelCapabilities,
    ModelSpec,
    ResultSpec,
    TargetCapabilities,
    TargetKind,
    TargetSpec,
    TraitSpec,
)


def identity(
    source: object,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> Identity:
    """Describe the Python name and optional presentation overrides."""
    python_name = getattr(source, "__name__", type(source).__name__)
    is_model = inspect.isclass(source) or not callable(source)
    resolved_name = (
        name
        if name is not None
        else (_snake_case(python_name) if is_model else python_name)
    )
    doc = getattr(source, "__doc__", None)
    return rename(
        Identity(
            python_name,
            resolved_name,
            _title(resolved_name),
            inspect.cleandoc(doc) if doc else "",
        ),
        title=title,
        description=description,
    )


def rename(
    definition: Identity,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> Identity:
    """Apply presentation overrides to an inspected identity."""
    return Identity(
        definition.python_name,
        name if name is not None else definition.name,
        title
        if title is not None
        else (_title(name) if name is not None else definition.title),
        description if description is not None else definition.description,
    )


def _title(name: str) -> str:
    return re.sub(
        r"\bAnywidget\b", "AnyWidget", _snake_case(name).replace("_", " ").title()
    )


def classify(source: object) -> TargetKind:
    """Identify instances, widget classes, and factories before compiling inputs."""
    if isinstance(source, AnyWidget) or (
        not inspect.isclass(source) and _is_protocol(source)
    ):
        return "instance"
    if inspect.isclass(source):
        if issubclass(source, AnyWidget):
            return "widget-class"
        raise TypeError(
            f"Widget target expected an AnyWidget subclass, got {source.__name__}"
        )
    if callable(source):
        return "factory"
    raise TypeError("Pass an AnyWidget instance, class, or a callable factory")


def inspect_source(source: object) -> TargetSpec:
    kind = classify(source)
    if kind == "instance":
        model = describe_model(source)
        call = None
    else:
        call = _callable_spec(
            cast(Callable[..., Any], source), widget_class=kind == "widget-class"
        )
        model = (
            describe_model(call.result.widget_type) if call.result.widget_type else None
        )
    return TargetSpec(
        source,
        kind,
        identity(source),
        call,
        model,
        TargetCapabilities(
            create=call is not None,
            read=model is not None and bool(model.capabilities.read),
            update=model is not None and bool(model.capabilities.write),
            observe=model is not None and bool(model.capabilities.observe),
        ),
    )


def describe_model(source: object) -> ModelSpec:
    """Inventory declared traits and observer capabilities without reading values."""
    is_class = inspect.isclass(source)
    if is_class and issubclass(source, t.HasTraits):
        traits = source.class_traits()
    else:
        reader = getattr(source, "traits", None)
        traits = cast(dict[str, Any], reader()) if callable(reader) else {}
    observable = callable(getattr(source, "observe", None)) and callable(
        getattr(source, "unobserve", None)
    )
    metadata_reader = getattr(source, "trait_metadata", None) if not is_class else None
    definitions = []
    for name, trait in traits.items():
        if not isinstance(trait, t.TraitType):
            continue
        metadata = dict(trait.metadata)
        if callable(metadata_reader):
            for key in ("to_json", "from_json"):
                value = metadata_reader(name, key)
                if value is not None or key in metadata:
                    metadata[key] = value
        synced = bool(metadata.get("sync"))
        custom_serialization = any(
            metadata.get(key) is not None for key in ("to_json", "from_json")
        )
        schema = (
            None if custom_serialization else trait_schema(trait, metadata=metadata)
        )
        restriction = (
            "unsynchronized"
            if not synced
            else "read-only"
            if trait.read_only
            else "custom-serialization"
            if custom_serialization
            else "unsupported-type"
            if schema is None
            else None
        )
        definitions.append(
            TraitSpec(
                name=name,
                type_name=type(trait).__name__,
                description=trait.help or "",
                synchronized=synced,
                read_only=trait.read_only,
                allow_none=trait.allow_none,
                default=trait.default_value,
                metadata=metadata,
                input_schema=JsonSchema(schema) if schema is not None else None,
                default_visible=synced
                and not name.startswith("_")
                and name not in {"layout", "tabbable", "tooltip"},
                write_restriction=restriction,
            )
        )
    observed = tuple(trait.name for trait in definitions) if observable else ()
    if observable and not definitions and not is_class:
        names = getattr(source, "trait_names", None)
        if callable(names):
            observed = tuple(cast(Sequence[str], names()))
    return ModelSpec(
        identity(source),
        tuple(definitions),
        ModelCapabilities(
            read=tuple(trait.name for trait in definitions),
            write=tuple(
                trait.name for trait in definitions if trait.write_restriction is None
            ),
            observe=observed,
        ),
    )


def _is_protocol(source: object) -> bool:
    # Descriptor access creates a comm and binds observers in AnyWidget.
    return isinstance(
        inspect.getattr_static(source, "_repr_mimebundle_", None),
        (MimeBundleDescriptor, ReprMimeBundle),
    )


def _callable_spec(source: Callable[..., Any], *, widget_class: bool) -> CallableSpec:
    signature = inspect.signature(source)
    annotation_source, globalns, localns = _annotation_namespace(source)
    raw_annotations = {
        parameter.name: parameter.annotation
        for parameter in signature.parameters.values()
        if parameter.annotation is not inspect.Parameter.empty
        and parameter.kind
        not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    }
    try:
        annotations = get_type_hints(
            types.SimpleNamespace(__annotations__=raw_annotations),
            globalns=globalns,
            localns=localns,
            include_extras=True,
        )
    except Exception as error:
        raise TypeError(
            f"Unable to evaluate type annotations for widget target {identity(source).python_name!r}"
        ) from error
    parameters = []
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise TypeError("Widget parameters must accept keyword arguments")
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            if widget_class:
                continue
            raise TypeError("Widget factory parameters must have explicit names")
        parameters.append(
            parameter.replace(
                annotation=annotations.get(parameter.name, parameter.annotation)
            )
        )
    result_annotation = signature.return_annotation
    if result_annotation is not inspect.Signature.empty:
        try:
            result_annotation = get_type_hints(
                types.SimpleNamespace(__annotations__={"return": result_annotation}),
                globalns=globalns,
                localns=localns,
                include_extras=True,
            )["return"]
        except Exception:
            # Local result types may become available only when the factory runs.
            pass
    result = (
        ResultSpec(result_annotation, cast(type, source), "single", False)
        if widget_class
        else _result_spec(result_annotation)
    )
    if (
        not widget_class
        and source is not annotation_source
        and (
            inspect.isgeneratorfunction(annotation_source)
            or inspect.isasyncgenfunction(annotation_source)
        )
    ):
        arguments = get_args(result_annotation)
        if (
            get_origin(result_annotation)
            in (Iterator, AsyncIterator, Generator, AsyncGenerator)
            and arguments
        ):
            inferred = _result_spec(arguments[0])
            result = ResultSpec(
                result_annotation, inferred.widget_type, inferred.cardinality, True
            )
    return CallableSpec(
        source,
        signature.replace(parameters=parameters, return_annotation=result_annotation),
        result,
        inspect.iscoroutinefunction(source)
        or inspect.iscoroutinefunction(annotation_source),
    )


def _annotation_namespace(
    source: Callable[..., Any],
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    target = source
    while isinstance(target, functools.partial):
        target = target.func
    target = inspect.unwrap(target)
    localns: dict[str, Any] = {}
    if inspect.isclass(target):
        for cls in reversed(target.__mro__):
            localns.update(vars(cls))
        localns[target.__name__] = target
        target = target.__init__
    elif not inspect.isfunction(target) and not inspect.ismethod(target):
        for cls in reversed(type(target).__mro__):
            localns.update(vars(cls))
        target = target.__call__
    if inspect.ismethod(target):
        owner = target.__self__
        owner_type = owner if inspect.isclass(owner) else type(owner)
        for cls in reversed(owner_type.__mro__):
            localns.update(vars(cls))
        target = target.__func__
    target = inspect.unwrap(target)
    module = sys.modules.get(getattr(target, "__module__", ""))
    globalns = dict(vars(module)) if module is not None else {}
    globalns.update(getattr(target, "__globals__", {}))
    return target, globalns, localns


def _result_spec(annotation: Any) -> ResultSpec:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return _result_spec(arguments[0])
    if inspect.isclass(annotation) and issubclass(annotation, AnyWidget):
        return ResultSpec(annotation, annotation, "single", False)
    if (
        origin
        in (AbstractContextManager, AbstractAsyncContextManager, Awaitable, Coroutine)
        and arguments
    ):
        child = _result_spec(arguments[-1] if origin is Coroutine else arguments[0])
        managed = (
            True
            if origin in (AbstractContextManager, AbstractAsyncContextManager)
            else child.managed
        )
        return ResultSpec(annotation, child.widget_type, child.cardinality, managed)
    if origin in (list, tuple, Sequence) and arguments:
        children = [_result_spec(item) for item in arguments if item is not Ellipsis]
        widget_types = {child.widget_type for child in children}
        widget_type = next(iter(widget_types)) if len(widget_types) == 1 else None
        return ResultSpec(annotation, widget_type, "sequence", False)
    if origin in (Union, types.UnionType) and arguments:
        children = [_result_spec(item) for item in arguments]
        widget_types = {child.widget_type for child in children}
        cardinalities: set[Literal["single", "sequence", "unknown"]] = {
            child.cardinality for child in children
        }
        management = {child.managed for child in children}
        return ResultSpec(
            annotation,
            next(iter(widget_types)) if len(widget_types) == 1 else None,
            next(iter(cardinalities)) if len(cardinalities) == 1 else "unknown",
            next(iter(management)) if len(management) == 1 else None,
        )
    return ResultSpec(annotation)


def _snake_case(name: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    value = re.sub(r"([a-z])([A-Z])", r"\1_\2", value)
    value = re.sub(r"([A-Za-z])([0-9]+)", r"\1_\2", value)
    return value.replace("-", "_").lower()
