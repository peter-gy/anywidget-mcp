"""Compile shared widget specifications into MCP App tools."""

from __future__ import annotations

import functools
import inspect
import json
import math
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence, Set
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Annotated, Any, Literal, TypeVar

from anywidget import AnyWidget
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from mcp.types import CallToolResult, Icon, ToolAnnotations
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    Field,
    Secret,
    SecretBytes,
    SecretStr,
)
from pydantic.fields import FieldInfo

from .._spec import compile_inputs, rename, scan
from .._spec.types import CallableSpec, InputSpec, JsonSchema, TargetSpec
from .._state import StateSpec, _DefaultState
from .._projection_json import MAX_SAFE_INTEGER

WidgetFactoryValue = AnyWidget | Sequence[AnyWidget]
WidgetFactoryResult = (
    WidgetFactoryValue
    | AbstractContextManager[WidgetFactoryValue]
    | AbstractAsyncContextManager[WidgetFactoryValue]
)
WidgetFactory = Callable[..., WidgetFactoryResult | Awaitable[WidgetFactoryResult]]
WidgetTarget = type[AnyWidget] | WidgetFactory
WidgetState = StateSpec | str | _DefaultState
ReopenMode = Literal["manual", "auto"]
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


class ReopenInputError(ToolError):
    """Creation inputs cannot be retained for a later invocation."""


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
        reopen: ReopenMode | None = None,
        reopen_ui: bool = False,
    ) -> None:
        if server._tool_manager.get_tool(self.tool.name) is not None:
            raise ValueError(f"Tool name {self.tool.name!r} is already registered")

        @functools.wraps(self.tool.fn)
        async def launch(**arguments: Any) -> CallToolResult:
            descriptor = (
                self.reopen_descriptor(arguments, reopen, ui=reopen_ui)
                if reopen
                else None
            )
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
            names = {
                field.alias or name: name
                for name, field in self.tool.fn_metadata.arg_model.model_fields.items()
            }
            arguments = {
                names.get(name, name): value for name, value in arguments.items()
            }
            result = await invoke(arguments, loading_message)
            if descriptor is not None:
                result.meta = {
                    **(result.meta or {}),
                    "anywidget": {"reopen": descriptor},
                }
            return result

        # The SDK's add_tool compiles a callable again. Copy the native Tool so
        # its validated schema, Context injection, and resolver plans stay paired.
        server._tool_manager._tools[self.tool.name] = self.tool.model_copy(
            update={"fn": launch, "meta": meta if meta is not None else self.tool.meta}
        )

    def reopen_descriptor(
        self, arguments: dict[str, Any], mode: ReopenMode, *, ui: bool
    ) -> dict[str, Any]:
        # The SDK has already validated these values and injected dependencies.
        # Construct only input fields, without running validators a second time.
        model = self.tool.fn_metadata.arg_model
        values = {
            name: arguments[field.alias or name]
            for name, field in model.model_fields.items()
        }
        snapshot = model.model_construct(**values)
        inputs: dict[str, Any] = {}
        for name in model.model_fields:
            if (
                self.inputs.synthetic_loading_message
                and name == LOADING_MESSAGE_ARGUMENT
            ):
                continue
            try:
                _validate_reopen_field(model, name, model.model_fields[name])
                _validate_reopen_values(values[name])
                serialized = snapshot.model_dump(
                    mode="json",
                    by_alias=True,
                    round_trip=True,
                    warnings="error",
                    include={name},
                )
                _validate_reopen_values(serialized, serialized=True)
                inputs.update(serialized)
            except (TypeError, ValueError, RecursionError, ReopenInputError) as error:
                reason = (
                    str(error)
                    if isinstance(error, ReopenInputError)
                    else "cannot be encoded as JSON"
                )
                raise ReopenInputError(
                    f"Cannot save reopening inputs for {self.tool.name!r}: argument {name!r} {reason}. "
                    "Use finite, non-secret JSON inputs, or pass a stable identifier and load data inside the factory."
                ) from error
        descriptor = {
            "version": 1,
            "tool": self.tool.name,
            "arguments": inputs,
            "mode": mode,
            "ui": ui,
        }
        try:
            encoded = json.dumps(
                descriptor, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as error:
            raise ReopenInputError(
                f"Cannot save reopening inputs for {self.tool.name!r}: the serialized inputs are not finite UTF-8 JSON. "
                "Use a stable identifier and load data inside the factory."
            ) from error
        byte_length = len(encoded)
        if byte_length > 64 * 1024:
            raise ReopenInputError(
                f"Cannot save reopening inputs for {self.tool.name!r}: creation metadata is {byte_length} bytes, "
                "exceeding the 65536-byte limit. Pass a stable identifier instead of the full data."
            )
        return json.loads(encoded)


def _validate_reopen_field(model: type[BaseModel], name: str, field: FieldInfo) -> None:
    if field.exclude:
        raise ReopenInputError(
            f"contains field {name!r} excluded from JSON. Remove exclude=True or use an identifier"
        )
    serialized_name = field.serialization_alias
    if serialized_name is None:
        serialized_name = field.alias if field.alias is not None else name
    alias = (
        field.validation_alias if field.validation_alias is not None else field.alias
    )
    aliases = alias.choices if isinstance(alias, AliasChoices) else [alias]
    accepts_name = alias is None or model.model_config.get("validate_by_name", False)
    accepts_alias = model.model_config.get("validate_by_alias", True) and any(
        candidate == serialized_name
        or (isinstance(candidate, AliasPath) and candidate.path == [serialized_name])
        for candidate in aliases
    )
    if not ((accepts_name and serialized_name == name) or accepts_alias):
        raise ReopenInputError(
            f"contains field {name!r} whose serialization name is not accepted by validation. "
            "Use matching input and output aliases"
        )


def _validate_reopen_values(value: Any, *, serialized: bool = False) -> None:
    if isinstance(value, (Secret, SecretStr, SecretBytes)):
        raise ReopenInputError("contains a secret value")
    if isinstance(value, float) and not math.isfinite(value):
        raise ReopenInputError("contains a non-finite number")
    if (
        serialized
        and isinstance(value, int)
        and not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER
    ):
        raise ReopenInputError(
            "contains an integer outside the browser's exact range. Use a string identifier"
        )
    if isinstance(value, BaseModel):
        for name, field in type(value).model_fields.items():
            _validate_reopen_field(type(value), name, field)
            _validate_reopen_values(getattr(value, name), serialized=serialized)
        _validate_reopen_values(value.model_extra, serialized=serialized)
    elif is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _validate_reopen_values(getattr(value, field.name), serialized=serialized)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _validate_reopen_values(key, serialized=serialized)
            _validate_reopen_values(item, serialized=serialized)
    elif isinstance(value, Iterator):
        raise ReopenInputError(
            "contains a one-shot iterator. Use a reusable collection"
        )
    elif isinstance(value, (Sequence, Set)) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        for item in value:
            _validate_reopen_values(item, serialized=serialized)


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
