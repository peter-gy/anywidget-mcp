"""Register AnyWidget MCP resources, tools, and their shared runtime lifespan."""

from __future__ import annotations

import math
import functools
import json
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Annotated, Any, Literal, TypedDict, overload

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, Icon, TextContent, ToolAnnotations
from pydantic import AnyUrl, Field

from ._attachments import BlobRef, CHUNK_BYTES, PROTOCOL_VERSION, validate_ref
from ._dynamic import WidgetCreationError
from ._projection_json import canonical_json
from ._runtime import (
    CommReplay,
    acknowledge_operations,
    PollReplay,
    SessionRuntime,
    SessionUnavailableError,
    comm_fingerprint,
    remember_comm_replay,
    protocol_error,
    model_result,
    session_result,
)
from ._state import DEFAULT_STATE, validate_state_spec
from ._mcp.targets import (
    SESSION_TOOL_NAMES,
    TargetT,
    WidgetState,
    MCPWidgetTarget,
    ReopenMode,
    prepare_target,
    normalize_state,
)

OperationId = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
AcknowledgedOperationId = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]

APP_MIME_TYPE = "text/html;profile=mcp-app"
APP_RESOURCE_URI = "ui://anywidget-mcp/app.html"


class AppCSP(TypedDict, total=False):
    """Content security policy metadata attached to the app resource."""

    connectDomains: list[str]
    resourceDomains: list[str]
    frameDomains: list[str]
    baseUriDomains: list[str]
    scriptDirectives: list[Literal["'unsafe-eval'", "'wasm-unsafe-eval'"]]


class AppPermissions(TypedDict, total=False):
    """Browser capability requests attached to the app resource."""

    camera: dict[str, Any]
    microphone: dict[str, Any]
    geolocation: dict[str, Any]
    clipboardWrite: dict[str, Any]


@dataclass
class _RuntimeGeneration:
    """Track references to one runtime shared by overlapping lifespans."""

    references: int = 1
    runtime: SessionRuntime | None = None
    closing: bool = False
    ready: anyio.Event = field(default_factory=anyio.Event)
    drained: anyio.Event = field(default_factory=anyio.Event)
    closed: anyio.Event = field(default_factory=anyio.Event)


class WidgetTools:
    """Register AnyWidget tools and share their runtime across server lifespans."""

    def __init__(
        self,
        mcp: MCPServer,
        *,
        app_uri: str = APP_RESOURCE_URI,
        csp: AppCSP | None = None,
        permissions: AppPermissions | None = None,
        prefers_border: bool = True,
        session_idle_timeout: float | None = 900.0,
    ) -> None:
        if session_idle_timeout is not None and (
            isinstance(session_idle_timeout, bool)
            or not isinstance(session_idle_timeout, (int, float))
            or not math.isfinite(session_idle_timeout)
            or session_idle_timeout <= 0
        ):
            raise ValueError(
                "session_idle_timeout must be None or a positive finite number"
            )
        app_uri = _validated_app_uri(mcp, app_uri)
        conflicts = [
            name for name in SESSION_TOOL_NAMES if mcp._tool_manager.get_tool(name)
        ]
        if conflicts:
            joined = ", ".join(sorted(conflicts))
            raise ValueError(
                f"MCPServer already defines reserved AnyWidget tools: {joined}"
            )
        self._mcp = mcp
        self._app_uri = app_uri
        self._csp = _app_csp(csp)
        self._permissions = permissions
        self._prefers_border = prefers_border
        self._session_idle_timeout = (
            float(session_idle_timeout) if session_idle_timeout is not None else None
        )
        self._runtime_lock = threading.RLock()
        self._runtime: SessionRuntime | None = None
        self._runtime_generation: _RuntimeGeneration | None = None
        self._register_app_resource()
        self._register_session_tools()
        self._install_lifespan()

    @overload
    def widget(
        self,
        target: TargetT,
        /,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        state: WidgetState = DEFAULT_STATE,
        reopen: ReopenMode | None = None,
        reopen_ui: bool | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
    ) -> TargetT: ...

    @overload
    def widget(
        self,
        target: None = None,
        /,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        state: WidgetState = DEFAULT_STATE,
        reopen: ReopenMode | None = None,
        reopen_ui: bool | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
    ) -> Callable[[TargetT], TargetT]: ...

    def widget(
        self,
        target: TargetT | None = None,
        /,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        state: WidgetState = DEFAULT_STATE,
        reopen: ReopenMode | None = None,
        reopen_ui: bool | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
    ) -> TargetT | Callable[[TargetT], TargetT]:
        """Register an AnyWidget class or factory as an MCP App tool.

        Args:
            target: An ``AnyWidget`` subclass or a callable that creates a fresh
                widget or a non-empty widget sequence. Omit it to use this method
                as a decorator.
            name: Tool name. Widget classes default to their snake-case class name.
            title: Human-facing tool title. Defaults to a title derived from the
                tool name.
            description: Tool description. Defaults to the target docstring.
            state: Model-visible state. Omit it for synchronized root traits, pass
                one trait name, a tuple of trait names, a ``StateProjection``, a
                projection callable, or ``None`` to disable model-context
                projection. Projection callables must not mutate synchronized
                widget traits.
            reopen: Allow fresh creation from saved inputs, manually or once on
                remount. The factory and its initialization must be safe to repeat.
            reopen_ui: Show built-in recovery controls. Defaults to True for
                manual reopening and False for automatic reopening.
            annotations: Standard MCP tool behavior hints.
            icons: Icons shown for the MCP tool.

        Returns:
            The registered target unchanged, or a decorator when ``target`` is
            omitted.

        Raises:
            TypeError: If the target, signature, or state option is invalid.
            ValueError: If the tool name is reserved or already registered.
            ToolError: If a tool invocation cannot create or open its widget.

        Target parameters define the widget-specific MCP inputs. When the target
        has no ``loading_message`` parameter, registration adds it as an optional
        host status argument and consumes it before target construction. A target
        parameter with that name keeps its own contract. MCPServer injects a
        ``Context`` parameter without exposing it in the schema. A factory may
        return one widget or a non-empty widget sequence, directly or through a
        synchronous or asynchronous context manager. A sequence renders in order
        and projects state as ``{"widgets": [state, ...]}``. The context manager
        remains active for the widget session. Factory acquisition is cancellable,
        so the manager owns rollback for resources acquired before it yields.

        Example:
            ``widgets.widget(ColorPicker, state="color")``
        """
        if reopen_ui is not None and not isinstance(reopen_ui, bool):
            raise TypeError("reopen_ui must be a bool or None")
        if reopen not in (None, "manual", "auto"):
            raise ValueError("reopen must be None, 'manual', or 'auto'")
        normalized_state = normalize_state(state)
        validate_state_spec(normalized_state)

        def register(candidate: TargetT) -> TargetT:
            compiled = prepare_target(
                candidate,
                name=name,
                title=title,
                description=description,
                annotations=annotations,
                icons=icons,
            )
            self._register_widget(
                compiled, state=normalized_state, reopen=reopen, reopen_ui=reopen_ui
            )
            return candidate

        if target is None:
            return register
        return register(target)

    def _register_widget(
        self,
        compiled: MCPWidgetTarget,
        *,
        state: WidgetState = DEFAULT_STATE,
        reopen: ReopenMode | None = None,
        reopen_ui: bool | None = None,
    ) -> None:
        normalized_state = normalize_state(state)
        validate_state_spec(normalized_state)

        creation = compiled.spec.call
        if creation is None:
            raise TypeError("Widget registration requires a creation capability")

        async def invoke(
            arguments: dict[str, Any], loading_message: str
        ) -> CallToolResult:
            try:
                return await self._require_runtime().open(
                    creation.invoke,
                    arguments,
                    normalized_state,
                    tool_name=compiled.spec.identity.name,
                    tool_title=compiled.spec.identity.title,
                    loading_message=loading_message,
                )
            except WidgetCreationError as error:
                raise ToolError(str(error)) from error

        compiled.register(
            self._mcp,
            invoke,
            meta={"ui": {"resourceUri": self._app_uri}},
            reopen=reopen,
            reopen_ui=(reopen == "manual" if reopen_ui is None else reopen_ui),
        )

    async def aclose(self) -> None:
        """Close every live widget session in the active server lifespan."""
        with self._runtime_lock:
            runtime = self._runtime
        if runtime is not None:
            await runtime.aclose()

    def _require_runtime(self) -> SessionRuntime:
        with self._runtime_lock:
            runtime = self._runtime
        if runtime is None:
            raise ToolError("The AnyWidget MCP server runtime is not active")
        return runtime

    def _install_lifespan(self) -> None:
        previous = self._mcp._lowlevel_server.lifespan

        @asynccontextmanager
        async def lifespan(server: Any):
            async with previous(server) as context:
                async with self._runtime_lifespan():
                    yield context

        self._mcp._lowlevel_server.lifespan = lifespan

    @asynccontextmanager
    async def _runtime_lifespan(self):
        """Borrow the active runtime generation or own its task group.

        The owning lifespan waits for every borrower before closing sessions.
        """

        generation, owner = await self._join_runtime_generation()
        if not owner:
            try:
                yield
            finally:
                self._release_runtime_generation(generation)
            return

        # AnyIO task groups must exit in the task that entered them. The first
        # lifespan owns that scope while later lifespans borrow its runtime.
        try:
            async with anyio.create_task_group() as task_group:
                runtime = SessionRuntime(
                    task_group,
                    session_idle_timeout=self._session_idle_timeout,
                    app_uri=self._app_uri,
                )
                self._activate_runtime_generation(generation, runtime)
                try:
                    yield
                finally:
                    with anyio.CancelScope(shield=True):
                        self._release_runtime_generation(generation)
                        await generation.drained.wait()
                        try:
                            await runtime.aclose()
                        finally:
                            task_group.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                self._finish_runtime_generation(generation)

    async def _join_runtime_generation(
        self,
    ) -> tuple[_RuntimeGeneration, bool]:
        while True:
            with self._runtime_lock:
                generation = self._runtime_generation
                if generation is None:
                    generation = _RuntimeGeneration()
                    self._runtime_generation = generation
                    return generation, True
                if generation.runtime is not None and not generation.closing:
                    generation.references += 1
                    return generation, False
                wait_for = generation.closed if generation.closing else generation.ready
            await wait_for.wait()

    def _activate_runtime_generation(
        self,
        generation: _RuntimeGeneration,
        runtime: SessionRuntime,
    ) -> None:
        with self._runtime_lock:
            if self._runtime_generation is not generation or generation.closing:
                raise RuntimeError("The AnyWidget MCP server runtime failed to start")
            generation.runtime = runtime
            self._runtime = runtime
            generation.ready.set()

    def _release_runtime_generation(
        self,
        generation: _RuntimeGeneration,
    ) -> None:
        with self._runtime_lock:
            if generation.references <= 0:
                raise RuntimeError("The AnyWidget MCP server lifespan is unbalanced")
            generation.references -= 1
            if generation.references == 0:
                generation.closing = True
                generation.drained.set()

    def _finish_runtime_generation(self, generation: _RuntimeGeneration) -> None:
        with self._runtime_lock:
            if generation.references:
                generation.references = 0
                generation.closing = True
                generation.drained.set()
            generation.ready.set()
            if self._runtime is generation.runtime:
                self._runtime = None
            if self._runtime_generation is generation:
                self._runtime_generation = None
            generation.closed.set()

    def _register_app_resource(self) -> None:
        ui_meta: dict[str, Any] = {
            "csp": self._csp,
            "prefersBorder": self._prefers_border,
        }
        if self._permissions:
            ui_meta["permissions"] = self._permissions

        @self._mcp.resource(
            self._app_uri,
            name="AnyWidget MCP App",
            mime_type=APP_MIME_TYPE,
            meta={"ui": ui_meta},
        )
        def anywidget_app() -> str:
            return (
                files("anywidget_mcp")
                .joinpath("static/index.html")
                .read_text(encoding="utf-8")
            )

    def _register_session_tools(self) -> None:
        """Register the model-state reader and browser protocol tools.

        Comm and poll calls serialize through each lease's protocol lock.
        Recorded operation IDs replay their stored outcome before new widget work.
        """

        app_only = {"ui": {"visibility": ["app"]}}
        model_only = {"ui": {"visibility": ["model"]}}

        async def anywidget_comm(
            instance_id: str,
            model_id: str,
            operation_id: OperationId,
            payload_ref: BlobRef,
            acknowledged_operation_id: AcknowledgedOperationId = 0,
        ) -> CallToolResult:
            runtime = self._require_runtime()
            with runtime.use(instance_id) as lease:
                with lease.protocol_lock:
                    acknowledge_operations(lease, acknowledged_operation_id)
                    fingerprint = comm_fingerprint(model_id, payload_ref)
                    replay = lease.comm_replays.get(operation_id)
                    if replay is not None:
                        lease.session.consume_uploads(operation_id)
                        if replay.fingerprint != fingerprint:
                            raise ToolError(
                                f"operation_id {operation_id!r} was reused for "
                                "another comm request"
                            )
                        if replay.error is not None:
                            raise ToolError(replay.error)
                        assert replay.result is not None
                        result = replay.result.model_copy(deep=True)
                        runtime.complete_bootstrap(lease)
                        return result

                    if operation_id <= lease.last_operation_sequence:
                        lease.session.consume_uploads(operation_id)
                        raise ToolError(
                            "Widget operation sequence was already completed or cancelled"
                        )
                    lease.session.retire_uploads_before(operation_id)
                    try:
                        value = json.loads(
                            lease.session.resolve_attachment(payload_ref)
                        )
                        if not isinstance(value, dict) or set(value) != {
                            "data",
                            "buffers",
                        }:
                            raise ValueError(
                                "Widget comm payload must contain data and buffers"
                            )
                        data, buffers = value["data"], value["buffers"]
                        if not isinstance(data, dict) or not isinstance(buffers, list):
                            raise ValueError(
                                "Widget comm data must be an object and buffers an array"
                            )
                        refs = [validate_ref(ref) for ref in buffers]
                        raw_buffers = [
                            lease.session.resolve_attachment(ref) for ref in refs
                        ]
                        snapshot = lease.session.receive(model_id, data, raw_buffers)
                        result, attachment_ids = session_result(lease, snapshot)
                    except Exception as error:
                        message = protocol_error(error)
                        remember_comm_replay(
                            lease,
                            operation_id,
                            CommReplay(
                                fingerprint=fingerprint,
                                result=None,
                                error=message,
                            ),
                        )
                        lease.session.consume_uploads(operation_id)
                        raise ToolError(message) from error
                    remember_comm_replay(
                        lease,
                        operation_id,
                        CommReplay(
                            fingerprint=fingerprint,
                            result=result.model_copy(deep=True),
                            error=None,
                            attachment_ids=attachment_ids,
                        ),
                    )
                    lease.session.consume_uploads(operation_id)
                    runtime.complete_bootstrap(lease)
                    return result

        async def anywidget_poll(
            instance_id: str,
            operation_id: OperationId,
            acknowledged_model_ids: list[str] | None = None,
            acknowledged_operation_id: AcknowledgedOperationId = 0,
        ) -> CallToolResult:
            runtime = self._require_runtime()
            with runtime.use(instance_id) as lease:
                with lease.protocol_lock:
                    if (
                        len(
                            canonical_json(acknowledged_model_ids or []).encode("utf-8")
                        )
                        > 96 * 1024
                    ):
                        raise ToolError(
                            "Widget removal acknowledgments exceed the request byte limit"
                        )
                    acknowledgments = tuple(
                        sorted(dict.fromkeys(acknowledged_model_ids or ()))
                    )
                    acknowledge_operations(lease, acknowledged_operation_id)
                    replay = lease.poll_replays.get(operation_id)
                    if replay is not None:
                        if replay.acknowledged_model_ids != acknowledgments:
                            raise ToolError(
                                f"operation_id {operation_id!r} was reused for "
                                "another poll request"
                            )
                        result = replay.result.model_copy(deep=True)
                        runtime.complete_bootstrap(lease)
                        return result

                    if operation_id <= lease.last_operation_sequence:
                        raise ToolError(
                            "Widget operation sequence was already completed or cancelled"
                        )
                    lease.session.retire_uploads_before(operation_id)
                    try:
                        lease.session.acknowledge_model_removals(acknowledgments)
                    except (KeyError, RuntimeError, ValueError) as error:
                        raise ToolError(protocol_error(error)) from error
                    snapshot = lease.session.snapshot()
                    result, attachment_ids = session_result(lease, snapshot)
                    replay = PollReplay(
                        operation_id=operation_id,
                        acknowledged_model_ids=acknowledgments,
                        result=result.model_copy(deep=True),
                        attachment_ids=attachment_ids,
                    )
                    replay.attachment_ids = lease.session.pin_attachments(
                        replay.attachment_ids
                    )
                    lease.poll_replays[operation_id] = replay
                    lease.last_operation_sequence = operation_id
                    lease.session.finish_delivery()
                    runtime.complete_bootstrap(lease)
                    return result

        async def anywidget_cancel(
            instance_id: str,
            operation_id: OperationId,
            acknowledged_operation_id: AcknowledgedOperationId = 0,
        ) -> CallToolResult:
            with self._require_runtime().use(instance_id) as lease:
                with lease.protocol_lock:
                    acknowledge_operations(lease, acknowledged_operation_id)
                    lease.last_operation_sequence = max(
                        lease.last_operation_sequence, operation_id
                    )
                    lease.session.consume_uploads(operation_id)
                    lease.session.retire_uploads_before(operation_id)
            return CallToolResult(
                content=[],
                _meta={
                    "anywidget": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "instanceId": instance_id,
                        "operationId": operation_id,
                        "retired": True,
                    }
                },
            )

        async def anywidget_dispose(
            session_id: str,
            operation_id: str | None = None,
        ) -> CallToolResult:
            disposed = await self._require_runtime().dispose(
                session_id,
                "app disposal",
                operation_id,
            )
            return CallToolResult(
                content=[],
                structured_content={"disposed": disposed},
            )

        async def anywidget_bootstrap(
            bootstrap_id: str,
            operation_id: str,
        ) -> CallToolResult:
            return self._require_runtime().bootstrap(bootstrap_id, operation_id)

        async def anywidget_state(
            state_id: Annotated[
                str,
                Field(
                    description=(
                        "The state_id in this widget's latest model context. "
                        "Reopening creates a new ID. If no context is available, "
                        "use the ID from its latest creation result."
                    )
                ),
            ],
        ) -> CallToolResult:
            """Read a widget's current state after user interaction.

            Call this before answering a question about the current state of an
            open widget. Prefer the ``state_id`` in this widget's latest model
            context over older tool results. Reopening creates a new ID. If no
            context is available, use the ID from its latest creation result.
            """
            tool_name, projection = self._require_runtime().state(state_id)
            return model_result(
                CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=(
                                f"Current {tool_name} state: "
                                f"{canonical_json(projection.state)}."
                            ),
                        )
                    ],
                    structured_content={
                        "state_id": state_id,
                        "tool": tool_name,
                        "version": projection.version,
                        "state": projection.state,
                    },
                )
            )

        async def anywidget_read(
            instance_id: str, blob_id: str, offset: int = 0
        ) -> CallToolResult:
            with self._require_runtime().use(instance_id) as lease:
                with lease.protocol_lock:
                    try:
                        payload = lease.session.read_attachment(blob_id, offset)
                    except (KeyError, RuntimeError, ValueError) as error:
                        raise ToolError(protocol_error(error)) from error
            return CallToolResult(content=[], _meta={"anywidget": payload})

        async def anywidget_write(
            instance_id: str,
            operation_id: OperationId,
            blob_id: str,
            byte_length: int,
            offset: int,
            data: Annotated[str, Field(max_length=4 * ((CHUNK_BYTES + 2) // 3))],
        ) -> CallToolResult:
            with self._require_runtime().use(instance_id) as lease:
                with lease.protocol_lock:
                    try:
                        if (
                            operation_id <= lease.last_operation_sequence
                            and operation_id not in lease.comm_replays
                        ):
                            raise ToolError(
                                "Widget operation sequence was already completed"
                            )
                        payload = lease.session.write_attachment(
                            operation_id, blob_id, byte_length, offset, data
                        )
                    except (KeyError, RuntimeError, ValueError) as error:
                        raise ToolError(protocol_error(error)) from error
            return CallToolResult(content=[], _meta={"anywidget": payload})

        state_annotations = ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
        self._mcp.add_tool(
            anywidget_state,
            annotations=state_annotations,
            meta=model_only,
            structured_output=False,
        )
        for tool in (
            anywidget_bootstrap,
            anywidget_read,
            anywidget_write,
            anywidget_comm,
            anywidget_poll,
            anywidget_dispose,
            anywidget_cancel,
        ):
            self._mcp.add_tool(
                _session_tool(tool), meta=app_only, structured_output=False
            )


def _session_tool(tool: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(tool)
    async def call(**arguments: Any) -> CallToolResult:
        try:
            return await tool(**arguments)
        except SessionUnavailableError as error:
            return CallToolResult(
                is_error=True,
                content=[TextContent(type="text", text=str(error))],
                _meta={"anywidget": {"error": "session_unavailable"}},
            )

    return call


def _validated_app_uri(mcp: MCPServer, app_uri: str) -> str:
    if not isinstance(app_uri, str):
        raise TypeError("app_uri must be a string")
    if "{" in app_uri or "}" in app_uri:
        raise ValueError("app_uri must be a concrete resource URI")
    try:
        normalized = str(AnyUrl(app_uri))
    except ValueError as error:
        raise ValueError(f"Invalid app_uri {app_uri!r}: {error}") from error
    occupied = {resource.uri for resource in mcp._resource_manager.list_resources()}
    if normalized in occupied:
        raise ValueError(f"MCPServer already defines the app resource {normalized!r}")
    return normalized


def _app_csp(csp: AppCSP | None) -> AppCSP:
    """Copy CSP metadata and permit blob-backed widget modules."""

    result: AppCSP = {}
    if csp is not None:
        if "connectDomains" in csp:
            result["connectDomains"] = list(csp["connectDomains"])
        if "resourceDomains" in csp:
            result["resourceDomains"] = list(csp["resourceDomains"])
        if "frameDomains" in csp:
            result["frameDomains"] = list(csp["frameDomains"])
        if "baseUriDomains" in csp:
            result["baseUriDomains"] = list(csp["baseUriDomains"])
        if "scriptDirectives" in csp:
            directives = csp["scriptDirectives"]
            if not isinstance(directives, list):
                raise TypeError("csp scriptDirectives must be a list")
            if any(
                not isinstance(directive, str)
                or directive not in ("'unsafe-eval'", "'wasm-unsafe-eval'")
                for directive in directives
            ):
                raise ValueError(
                    "csp scriptDirectives accepts 'unsafe-eval' and 'wasm-unsafe-eval'"
                )
            result["scriptDirectives"] = list(directives)
    resources = result.setdefault("resourceDomains", [])
    if "blob:" not in resources:
        resources.append("blob:")
    return result
