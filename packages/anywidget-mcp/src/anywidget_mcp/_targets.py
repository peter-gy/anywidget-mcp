from __future__ import annotations

import functools
import inspect
import re
import sys
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypeVar, cast, get_type_hints

from anywidget import AnyWidget
from mcp.server.fastmcp.tools import Tool
from mcp.types import CallToolResult, Icon, ToolAnnotations
from pydantic import Field

from ._state import DEFAULT_STATE, StateSpec, _DefaultState

WidgetFactoryValue = AnyWidget | Sequence[AnyWidget]
WidgetFactoryResult = (
    WidgetFactoryValue
    | AbstractContextManager[WidgetFactoryValue]
    | AbstractAsyncContextManager[WidgetFactoryValue]
)
WidgetFactory = Callable[..., WidgetFactoryResult | Awaitable[WidgetFactoryResult]]
WidgetTarget = type[AnyWidget] | WidgetFactory
WidgetTargetKind = Literal["widget-class", "factory"]
WidgetState = StateSpec | str | _DefaultState
TargetT = TypeVar("TargetT", bound=Callable[..., Any])

SESSION_TOOL_NAMES = frozenset(
    {
        "anywidget_bootstrap",
        "anywidget_assets",
        "anywidget_comm",
        "anywidget_poll",
        "anywidget_state",
        "anywidget_dispose",
    }
)
LOADING_MESSAGE_ARGUMENT = "loading_message"
LOADING_MESSAGE_DESCRIPTION = (
    "Progress text shown while this widget initializes. "
    "Describe the current request in at most 120 characters."
)
MAX_LOADING_MESSAGE_LENGTH = 120


@dataclass(frozen=True)
class WidgetTargetDescription:
    target_name: str
    tool_name: str
    title: str
    description: str
    kind: WidgetTargetKind
    signature: inspect.Signature
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class CompiledWidgetTarget:
    description: WidgetTargetDescription
    tool: Tool
    invocation_adapter: Callable[..., Awaitable[CallToolResult]] = field(
        repr=False,
        compare=False,
    )
    _bind_invocation: Callable[
        [Callable[[dict[str, Any], str], Awaitable[CallToolResult]]],
        None,
    ] = field(repr=False, compare=False)

    def bind(
        self,
        invoke: Callable[[dict[str, Any], str], Awaitable[CallToolResult]],
    ) -> Callable[..., Awaitable[CallToolResult]]:
        """Bind the compiled public signature to one runtime invocation."""
        self._bind_invocation(invoke)
        return self.invocation_adapter


def describe_widget_target(
    candidate: object,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> WidgetTargetDescription:
    """Validate a widget target and return its MCP tool description."""
    return compile_widget_target(
        candidate,
        name=name,
        title=title,
        description=description,
    ).description


def compile_widget_target(
    candidate: object,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    annotations: ToolAnnotations | None = None,
    icons: list[Icon] | None = None,
    meta: dict[str, Any] | None = None,
) -> CompiledWidgetTarget:
    if isinstance(candidate, AnyWidget):
        raise TypeError(
            f"widgets.widget() received a {type(candidate).__name__} instance. "
            f"Pass the {type(candidate).__name__} class or a factory that creates "
            "a fresh widget for every tool call."
        )

    is_widget_class = inspect.isclass(candidate) and issubclass(candidate, AnyWidget)
    if inspect.isclass(candidate) and not is_widget_class:
        raise TypeError(
            f"widgets.widget() expected an AnyWidget subclass, got {candidate.__name__}"
        )
    if not callable(candidate):
        raise TypeError(
            "widgets.widget() expected an AnyWidget subclass or a callable factory"
        )

    target = cast(Callable[..., Any], candidate)
    target_name = getattr(target, "__name__", type(target).__name__)
    tool_name = name or (_snake_case(target_name) if is_widget_class else target_name)
    if tool_name in SESSION_TOOL_NAMES:
        raise ValueError(
            f"Widget tool name {tool_name!r} is reserved for the AnyWidget app"
        )
    tool_title = title if title is not None else _tool_title(tool_name)
    tool_description = (
        description if description is not None else (_direct_doc(target) or "")
    )
    default_loading_message = _default_loading_message(tool_title)
    signature, synthetic_loading_message = _tool_signature(
        target,
        is_widget_class=is_widget_class,
        loading_message=default_loading_message,
    )
    kind: WidgetTargetKind = "widget-class" if is_widget_class else "factory"
    invocation_adapter, bind_invocation = _invocation_adapter(
        target,
        signature,
        target_name=target_name,
        description=tool_description,
        synthetic_loading_message=synthetic_loading_message,
        default_loading_message=default_loading_message,
    )
    try:
        tool = Tool.from_function(
            invocation_adapter,
            name=tool_name,
            title=tool_title,
            description=tool_description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=False,
        )
    except Exception as error:
        raise TypeError(
            f"Could not compile widget target {target_name!r}: {error}"
        ) from error
    target_description = WidgetTargetDescription(
        target_name=target_name,
        tool_name=tool_name,
        title=tool_title,
        description=tool_description,
        kind=kind,
        signature=signature,
        input_schema=tool.parameters,
    )
    return CompiledWidgetTarget(
        description=target_description,
        tool=tool,
        invocation_adapter=invocation_adapter,
        _bind_invocation=bind_invocation,
    )


def normalize_state(state: WidgetState) -> StateSpec | _DefaultState:
    return (state,) if isinstance(state, str) else state


def _invocation_adapter(
    target: Callable[..., Any],
    signature: inspect.Signature,
    *,
    target_name: str,
    description: str,
    synthetic_loading_message: bool,
    default_loading_message: str,
) -> tuple[
    Callable[..., Awaitable[CallToolResult]],
    Callable[[Callable[[dict[str, Any], str], Awaitable[CallToolResult]]], None],
]:
    bound: list[Callable[[dict[str, Any], str], Awaitable[CallToolResult]] | None] = [
        None
    ]

    @functools.wraps(target)
    async def launch(**arguments: Any) -> CallToolResult:
        invoke = bound[0]
        if invoke is None:
            raise RuntimeError("The compiled widget target is not registered")
        loading_value = arguments.get(LOADING_MESSAGE_ARGUMENT)
        loading_parameter = signature.parameters[LOADING_MESSAGE_ARGUMENT]
        if (
            loading_value is None
            and loading_parameter.default is not inspect.Parameter.empty
        ):
            loading_value = loading_parameter.default
        loading_message = _normalize_loading_message(
            loading_value,
            fallback=default_loading_message,
        )
        if synthetic_loading_message:
            arguments.pop(LOADING_MESSAGE_ARGUMENT, None)
        return await invoke(arguments, loading_message)

    launch.__name__ = target_name
    launch.__doc__ = description
    compiled_signature = signature.replace(return_annotation=CallToolResult)
    launch.__annotations__ = {
        parameter.name: parameter.annotation
        for parameter in compiled_signature.parameters.values()
        if parameter.annotation is not inspect.Parameter.empty
    }
    launch.__annotations__["return"] = CallToolResult
    setattr(launch, "__signature__", compiled_signature)

    def bind(
        invoke: Callable[[dict[str, Any], str], Awaitable[CallToolResult]],
    ) -> None:
        if bound[0] is not None:
            raise RuntimeError("The compiled widget target is already registered")
        bound[0] = invoke

    return launch, bind


def _tool_signature(
    candidate: Callable[..., Any],
    *,
    is_widget_class: bool,
    loading_message: str,
) -> tuple[inspect.Signature, bool]:
    signature = inspect.signature(candidate)
    globalns, localns = _annotation_namespace(candidate)
    raw_annotations = {
        parameter.name: parameter.annotation
        for parameter in signature.parameters.values()
        if parameter.annotation is not inspect.Parameter.empty
    }

    def annotation_target() -> None:
        pass

    annotation_target.__annotations__ = raw_annotations
    try:
        annotations = get_type_hints(
            annotation_target,
            globalns=globalns,
            localns=localns,
            include_extras=True,
        )
    except Exception as error:
        target_name = getattr(candidate, "__name__", type(candidate).__name__)
        raise TypeError(
            f"Unable to evaluate type annotations for widget target {target_name!r}"
        ) from error

    parameters: list[inspect.Parameter] = []
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise TypeError("Widget parameters must accept keyword arguments")
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            if is_widget_class:
                continue
            raise TypeError("Widget factory parameters must have explicit names")
        if parameter.name in annotations:
            parameter = parameter.replace(annotation=annotations[parameter.name])
        parameters.append(parameter)
    synthetic_loading_message = not any(
        parameter.name == LOADING_MESSAGE_ARGUMENT for parameter in parameters
    )
    if synthetic_loading_message:
        parameters.append(
            inspect.Parameter(
                LOADING_MESSAGE_ARGUMENT,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=loading_message,
                annotation=Annotated[
                    str,
                    Field(description=LOADING_MESSAGE_DESCRIPTION),
                ],
            )
        )
    return signature.replace(parameters=parameters), synthetic_loading_message


def _annotation_namespace(
    candidate: Callable[..., Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    target = candidate
    while isinstance(target, functools.partial):
        target = target.func
    target = inspect.unwrap(target)
    localns: dict[str, Any] = {}
    if inspect.isclass(target):
        localns.update(vars(target))
        target = target.__init__
    elif not inspect.isfunction(target) and not inspect.ismethod(target):
        target = target.__call__
    if inspect.ismethod(target):
        target = target.__func__
    module = sys.modules.get(getattr(target, "__module__", ""))
    globalns = dict(vars(module)) if module is not None else {}
    globalns.update(getattr(target, "__globals__", {}))
    return globalns, localns


def _snake_case(name: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    value = re.sub(r"([a-z])([A-Z])", r"\1_\2", value)
    value = re.sub(r"([A-Za-z])([0-9]+)", r"\1_\2", value)
    return value.replace("-", "_").lower()


def _direct_doc(candidate: Callable[..., Any]) -> str | None:
    doc = getattr(candidate, "__doc__", None)
    return inspect.cleandoc(doc) if doc else None


def _tool_title(name: str) -> str:
    title = _snake_case(name).replace("_", " ").title()
    return re.sub(r"\bAnywidget\b", "AnyWidget", title)


def _default_loading_message(title: str) -> str:
    return _normalize_loading_message(
        f"Initializing {title}…",
        fallback="Initializing widget…",
    )


def _normalize_loading_message(value: Any, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized or len(normalized) > MAX_LOADING_MESSAGE_LENGTH:
        return fallback
    if any(
        unicodedata.category(character) == "Cc"
        or "\u202a" <= character <= "\u202e"
        or "\u2066" <= character <= "\u2069"
        for character in normalized
    ):
        return fallback
    return normalized


__all__ = [
    "DEFAULT_STATE",
    "CompiledWidgetTarget",
    "TargetT",
    "WidgetState",
    "WidgetTarget",
    "WidgetTargetDescription",
    "compile_widget_target",
    "describe_widget_target",
    "normalize_state",
]
