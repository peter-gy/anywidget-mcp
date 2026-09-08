"""Compile shared widget specifications into MCP App tools."""

from __future__ import annotations

import functools
import inspect
import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, replace
from typing import Annotated, Any, TypeVar

from anywidget import AnyWidget
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.types import CallToolResult, Icon, ToolAnnotations
from pydantic import Field

from .._spec import compile_inputs, rename, scan
from .._spec.types import CallableSpec, InputSpec, JsonSchema, TargetSpec
from .._state import StateSpec, _DefaultState

WidgetFactoryValue = AnyWidget | Sequence[AnyWidget]
WidgetFactoryResult = (
    WidgetFactoryValue
    | AbstractContextManager[WidgetFactoryValue]
    | AbstractAsyncContextManager[WidgetFactoryValue]
)
WidgetFactory = Callable[..., WidgetFactoryResult | Awaitable[WidgetFactoryResult]]
WidgetTarget = type[AnyWidget] | WidgetFactory
WidgetState = StateSpec | str | _DefaultState
TargetT = TypeVar("TargetT", bound=Callable[..., Any])
Invocation = Callable[[dict[str, Any], str], Awaitable[CallToolResult]]

SESSION_TOOL_NAMES = frozenset(
    {
        "anywidget_bootstrap",
        "anywidget_read",
        "anywidget_write",
        "anywidget_comm",
        "anywidget_poll",
        "anywidget_state",
        "anywidget_dispose",
        "anywidget_cancel",
    }
)
LOADING_MESSAGE_ARGUMENT = "loading_message"
LOADING_MESSAGE_DESCRIPTION = (
    "Progress text shown while this widget initializes. "
    "Describe the current request in at most 120 characters."
)
MAX_LOADING_MESSAGE_LENGTH = 120


@dataclass(frozen=True)
class MCPInputs(InputSpec):
    tool: Tool
    synthetic_loading_message: bool
    default_loading_message: str


@dataclass(frozen=True)
class MCPWidgetTarget:
    spec: TargetSpec
    inputs: MCPInputs

    @property
    def tool(self) -> Tool:
        return self.inputs.tool

    def register(
        self,
        server: MCPServer,
        invoke: Invocation,
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if server._tool_manager.get_tool(self.tool.name) is not None:
            raise ValueError(f"Tool name {self.tool.name!r} is already registered")

        @functools.wraps(self.tool.fn)
        async def launch(**arguments: Any) -> CallToolResult:
            loading_value = arguments.get(LOADING_MESSAGE_ARGUMENT)
            loading_parameter = self.inputs.signature.parameters[
                LOADING_MESSAGE_ARGUMENT
            ]
            if (
                loading_value is None
                and loading_parameter.default is not inspect.Parameter.empty
            ):
                loading_value = loading_parameter.default
            loading_message = _normalize_loading_message(
                loading_value, fallback=self.inputs.default_loading_message
            )
            if self.inputs.synthetic_loading_message:
                arguments.pop(LOADING_MESSAGE_ARGUMENT, None)
            return await invoke(arguments, loading_message)

        # The SDK's add_tool compiles a callable again. Copy the native Tool so
        # its validated schema, Context injection, and resolver plans stay paired.
        server._tool_manager._tools[self.tool.name] = self.tool.model_copy(
            update={"fn": launch, "meta": meta if meta is not None else self.tool.meta}
        )


def prepare_target(
    source: object,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    annotations: ToolAnnotations | None = None,
    icons: list[Icon] | None = None,
    meta: dict[str, Any] | None = None,
) -> MCPWidgetTarget:
    spec = scan(source)
    spec = replace(
        spec,
        identity=rename(
            spec.identity, name=name or None, title=title, description=description
        ),
    )
    return compile_mcp_target(spec, annotations=annotations, icons=icons, meta=meta)


def compile_mcp_target(
    spec: TargetSpec,
    *,
    annotations: ToolAnnotations | None = None,
    icons: list[Icon] | None = None,
    meta: dict[str, Any] | None = None,
) -> MCPWidgetTarget:
    if spec.kind == "instance":
        name = spec.identity.python_name
        raise TypeError(
            f"widgets.widget() received a {name} instance. "
            f"Pass the {name} class or a factory that creates "
            "a fresh widget for every tool call."
        )
    if spec.identity.name in SESSION_TOOL_NAMES:
        raise ValueError(
            f"Widget tool name {spec.identity.name!r} is reserved for the AnyWidget app"
        )
    compiler = _MCPInputCompiler(spec, annotations, icons, meta)
    try:
        inputs = compile_inputs(spec, compiler)
    except Exception as error:
        raise TypeError(
            f"Could not compile widget target {spec.identity.python_name!r}: {error}"
        ) from error
    return MCPWidgetTarget(spec, inputs)


@dataclass(frozen=True)
class _MCPInputCompiler:
    spec: TargetSpec
    annotations: ToolAnnotations | None
    icons: list[Icon] | None
    meta: dict[str, Any] | None

    def __call__(self, definition: CallableSpec) -> MCPInputs:
        default_loading_message = _default_loading_message(self.spec.identity.title)
        signature = definition.signature
        synthetic = LOADING_MESSAGE_ARGUMENT not in signature.parameters
        if synthetic:
            signature = signature.replace(
                parameters=[
                    *definition.parameters,
                    inspect.Parameter(
                        LOADING_MESSAGE_ARGUMENT,
                        kind=inspect.Parameter.KEYWORD_ONLY,
                        default=default_loading_message,
                        annotation=Annotated[
                            str, Field(description=LOADING_MESSAGE_DESCRIPTION)
                        ],
                    ),
                ]
            )

        @functools.wraps(definition.invoke)
        async def launch(**arguments: Any) -> CallToolResult:
            raise RuntimeError("The compiled widget target is not registered")

        launch.__name__ = self.spec.identity.python_name
        launch.__doc__ = self.spec.identity.description
        compiled_signature = signature.replace(return_annotation=CallToolResult)
        launch.__annotations__ = {
            parameter.name: parameter.annotation
            for parameter in compiled_signature.parameters.values()
            if parameter.annotation is not inspect.Parameter.empty
        }
        launch.__annotations__["return"] = CallToolResult
        setattr(launch, "__signature__", compiled_signature)
        tool = Tool.from_function(
            launch,
            name=self.spec.identity.name,
            title=self.spec.identity.title,
            description=self.spec.identity.description,
            annotations=self.annotations,
            icons=self.icons,
            meta=self.meta,
            structured_output=False,
        )
        return MCPInputs(
            signature,
            JsonSchema(tool.parameters),
            tool.fn_metadata.validate_arguments,
            tool,
            synthetic,
            default_loading_message,
        )


def normalize_state(state: WidgetState) -> StateSpec | _DefaultState:
    return (state,) if isinstance(state, str) else state


def _default_loading_message(title: str) -> str:
    return _normalize_loading_message(
        f"Initializing {title}…",
        fallback="Initializing widget…",
    )


def _normalize_loading_message(value: Any, *, fallback: str) -> str:
    """Return safe one-line status text or the registered fallback."""

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
