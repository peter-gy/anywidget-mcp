"""Register AnyWidget MCP resources, tools, and their shared runtime lifespan."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Annotated, Any, TypedDict, cast, overload

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, Icon, TextContent, ToolAnnotations
from pydantic import AnyUrl, Field
from starlette.applications import Starlette
from starlette.types import Lifespan

from ._bridge import PROTOCOL_VERSION
from ._projection_json import canonical_json
from ._runtime import (
    CommReplay,
    PollReplay,
    SessionRuntime,
    comm_fingerprint,
    remember_comm_replay,
    session_result,
    validate_operation_id,
)
from ._state import DEFAULT_STATE, validate_state_spec
from ._targets import (
    SESSION_TOOL_NAMES,
    TargetT,
    WidgetState,
    compile_widget_target,
    normalize_state,
)

APP_MIME_TYPE = "text/html;profile=mcp-app"
APP_RESOURCE_URI = "ui://anywidget-mcp/app.html"


class AppCSP(TypedDict, total=False):
    """Content security policy metadata attached to the app resource."""

    connectDomains: list[str]
    resourceDomains: list[str]
    frameDomains: list[str]
    baseUriDomains: list[str]


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
        mcp: FastMCP,
        *,
        app_uri: str = APP_RESOURCE_URI,
        csp: AppCSP | None = None,
        permissions: AppPermissions | None = None,
        prefers_border: bool = True,
        session_idle_timeout: float = 900.0,
    ) -> None:
        if (
            isinstance(session_idle_timeout, bool)
            or not isinstance(session_idle_timeout, (int, float))
            or not math.isfinite(session_idle_timeout)
            or session_idle_timeout <= 0
        ):
            raise ValueError("session_idle_timeout must be a positive finite number")
        app_uri = _validated_app_uri(mcp, app_uri)
        conflicts = [
            name for name in SESSION_TOOL_NAMES if mcp._tool_manager.get_tool(name)
        ]
        if conflicts:
            joined = ", ".join(sorted(conflicts))
            raise ValueError(
                f"FastMCP already defines reserved AnyWidget tools: {joined}"
            )
        self._mcp = mcp
        self._app_uri = app_uri
        self._csp = _app_csp(csp)
        self._permissions = permissions
        self._prefers_border = prefers_border
        self._session_idle_timeout = float(session_idle_timeout)
        self._runtime_lock = threading.RLock()
        self._runtime: SessionRuntime | None = None
        self._runtime_generation: _RuntimeGeneration | None = None
        self._register_app_resource()
        self._register_session_tools()
        self._install_lifespan()
        self._install_streamable_http_app()

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
        parameter with that name keeps its own contract. FastMCP injects a
        ``Context`` parameter without exposing it in the schema. A factory may
        return one widget or a non-empty widget sequence, directly or through a
        synchronous or asynchronous context manager. A sequence renders in order
        and projects state as ``{"widgets": [state, ...]}``. The context manager
        remains active for the widget session. Factory acquisition is cancellable,
        so the manager owns rollback for resources acquired before it yields.

        Example:
            ``widgets.widget(ColorPicker, state="color")``
        """
        normalized_state = normalize_state(state)
        validate_state_spec(normalized_state)

        def register(candidate: TargetT) -> TargetT:
            tool_meta = {"ui": {"resourceUri": self._app_uri}}
            compiled = compile_widget_target(
                candidate,
                name=name,
                title=title,
                description=description,
                annotations=annotations,
                icons=icons,
                meta=tool_meta,
            )
            target_description = compiled.description
            tool_name = target_description.tool_name
            tool_title = target_description.title
            if self._mcp._tool_manager.get_tool(tool_name) is not None:
                raise ValueError(f"Tool name {tool_name!r} is already registered")

            async def invoke(
                arguments: dict[str, Any],
                loading_message: str,
            ) -> CallToolResult:
                return await self._require_runtime().open(
                    candidate,
                    arguments,
                    normalized_state,
                    tool_name=tool_name,
                    tool_title=tool_title,
                    loading_message=loading_message,
                )

            launch = compiled.bind(invoke)
            self._mcp.add_tool(
                launch,
                name=compiled.tool.name,
                title=compiled.tool.title,
                description=compiled.tool.description,
                annotations=compiled.tool.annotations,
                icons=compiled.tool.icons,
                meta=compiled.tool.meta,
                structured_output=False,
            )
            return candidate

        if target is None:
            return register
        return register(target)

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
        previous = self._mcp._mcp_server.lifespan

        @asynccontextmanager
        async def lifespan(server: Any):
            async with previous(server) as context:
                async with self._runtime_lifespan():
                    yield context

        self._mcp._mcp_server.lifespan = lifespan

    def _install_streamable_http_app(self) -> None:
        """Keep sessions alive while Streamable HTTP connections rotate.

        FastMCP enters its low-level lifespan per connection. The outer ASGI
        lifespan retains the shared runtime across those connections.
        """

        create_app = self._mcp.streamable_http_app

        def streamable_http_app() -> Starlette:
            app = create_app()
            previous_lifespan = app.router.lifespan_context

            @asynccontextmanager
            async def lifespan(starlette_app: Starlette):
                async with self._runtime_lifespan():
                    async with previous_lifespan(starlette_app) as state:
                        yield state

            app.router.lifespan_context = cast(Lifespan[Starlette], lifespan)
            return app

        setattr(self._mcp, "streamable_http_app", streamable_http_app)

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
                .joinpath("static", "index.html")
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
            data: dict[str, Any],
            operation_id: str,
            buffers: list[str] | None = None,
        ) -> CallToolResult:
            runtime = self._require_runtime()
            with runtime.use(instance_id) as lease:
                with lease.protocol_lock:
                    fingerprint = comm_fingerprint(model_id, data, buffers or ())
                    validate_operation_id(operation_id)
                    replay = lease.comm_replays.get(operation_id)
                    if replay is not None:
                        if replay.fingerprint != fingerprint:
                            raise ToolError(
                                f"operation_id {operation_id!r} was reused for "
                                "another comm request"
                            )
                        lease.comm_replays.move_to_end(operation_id)
                        if replay.error is not None:
                            raise ToolError(replay.error)
                        assert replay.result is not None
                        result = replay.result.model_copy(deep=True)
                        runtime.complete_bootstrap(lease)
                        return result

                    try:
                        snapshot = lease.session.receive(model_id, data, buffers or ())
                        result = session_result(lease, snapshot)
                    except Exception as error:
                        message = str(error)
                        remember_comm_replay(
                            lease,
                            operation_id,
                            CommReplay(
                                fingerprint=fingerprint,
                                result=None,
                                error=message,
                            ),
                        )
                        raise ToolError(message) from error
                    remember_comm_replay(
                        lease,
                        operation_id,
                        CommReplay(
                            fingerprint=fingerprint,
                            result=result.model_copy(deep=True),
                            error=None,
                            asset_ids=tuple(snapshot.asset_manifest),
                        ),
                    )
                    runtime.complete_bootstrap(lease)
                    return result

        async def anywidget_poll(
            instance_id: str,
            operation_id: str,
            acknowledged_model_ids: list[str] | None = None,
        ) -> CallToolResult:
            runtime = self._require_runtime()
            with runtime.use(instance_id) as lease:
                with lease.protocol_lock:
                    validate_operation_id(operation_id)
                    acknowledgments = tuple(
                        sorted(dict.fromkeys(acknowledged_model_ids or ()))
                    )
                    replay = lease.last_poll_replay
                    if replay is not None and replay.operation_id == operation_id:
                        if replay.acknowledged_model_ids != acknowledgments:
                            raise ToolError(
                                f"operation_id {operation_id!r} was reused for "
                                "another poll request"
                            )
                        result = replay.result.model_copy(deep=True)
                        runtime.complete_bootstrap(lease)
                        return result

                    try:
                        lease.session.acknowledge_model_removals(acknowledgments)
                    except (KeyError, RuntimeError, ValueError) as error:
                        raise ToolError(str(error)) from error
                    snapshot = lease.session.snapshot()
                    result = session_result(lease, snapshot)
                    replay = PollReplay(
                        operation_id=operation_id,
                        acknowledged_model_ids=acknowledgments,
                        result=result.model_copy(deep=True),
                        asset_ids=tuple(snapshot.asset_manifest),
                    )
                    replay.asset_ids = lease.session.pin_assets(replay.asset_ids)
                    previous = lease.last_poll_replay
                    lease.last_poll_replay = replay
                    if previous is not None:
                        lease.session.release_assets(previous.asset_ids)
                    runtime.complete_bootstrap(lease)
                    return result

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
                structuredContent={"disposed": disposed},
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
                        "The state_id returned by the widget tool that opened the app."
                    )
                ),
            ],
        ) -> CallToolResult:
            """Read a widget's current state after user interaction.

            Call this before answering a question about the current state of an
            open widget. Pass the ``state_id`` returned by its widget tool.
            """
            tool_name, projection = self._require_runtime().state(state_id)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            f"Current {tool_name} state: "
                            f"{canonical_json(projection.state)}."
                        ),
                    )
                ],
                structuredContent={
                    "state_id": state_id,
                    "tool": tool_name,
                    "version": projection.version,
                    "state": projection.state,
                },
            )

        async def anywidget_assets(
            instance_id: str,
            asset_ids: list[str],
        ) -> CallToolResult:
            runtime = self._require_runtime()
            with runtime.use(instance_id) as lease:
                with lease.protocol_lock:
                    try:
                        contents = lease.session.asset_contents(asset_ids)
                    except (KeyError, RuntimeError, ValueError) as error:
                        raise ToolError(str(error)) from error
                    runtime.complete_bootstrap(lease)
            return CallToolResult(
                content=[],
                _meta={
                    "anywidget": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "assetContents": contents,
                    }
                },
            )

        state_annotations = ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
        self._mcp.add_tool(anywidget_bootstrap, meta=app_only, structured_output=False)
        self._mcp.add_tool(
            anywidget_state,
            annotations=state_annotations,
            meta=model_only,
            structured_output=False,
        )
        self._mcp.add_tool(anywidget_assets, meta=app_only, structured_output=False)
        self._mcp.add_tool(anywidget_comm, meta=app_only, structured_output=False)
        self._mcp.add_tool(anywidget_poll, meta=app_only, structured_output=False)
        self._mcp.add_tool(anywidget_dispose, meta=app_only, structured_output=False)


def _validated_app_uri(mcp: FastMCP, app_uri: str) -> str:
    if not isinstance(app_uri, str):
        raise TypeError("app_uri must be a string")
    if "{" in app_uri or "}" in app_uri:
        raise ValueError("app_uri must be a concrete resource URI")
    try:
        normalized = str(AnyUrl(app_uri))
    except ValueError as error:
        raise ValueError(f"Invalid app_uri {app_uri!r}: {error}") from error
    occupied = {
        str(resource.uri) for resource in mcp._resource_manager.list_resources()
    }
    if normalized in occupied:
        raise ValueError(f"FastMCP already defines the app resource {normalized!r}")
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
    resources = result.setdefault("resourceDomains", [])
    if "blob:" not in resources:
        resources.append("blob:")
    return result
