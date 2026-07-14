from __future__ import annotations

import functools
import inspect
import json
import logging
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, Literal, TypeVar, TypedDict, cast, overload

from anywidget import AnyWidget
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import Tool
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ._bridge import SessionSnapshot, WidgetInUseError, WidgetSession
from ._state import (
    DEFAULT_STATE,
    ProjectionUpdate,
    StateSpec,
    _DefaultState,
    validate_state_spec,
)

logger = logging.getLogger(__name__)

APP_MIME_TYPE = "text/html;profile=mcp-app"
APP_RESOURCE_URI = "ui://anywidget-mcp/widget.html"
_SESSION_TOOL_NAMES = frozenset(
    {"anywidget_comm", "anywidget_poll", "anywidget_dispose"}
)

WidgetFactory = Callable[..., AnyWidget | Awaitable[AnyWidget]]
WidgetTarget = type[AnyWidget] | WidgetFactory
TargetT = TypeVar("TargetT", bound=Callable[..., Any])
Transport = Literal["streamable-http", "stdio"]
RunTransport = Literal["stdio", "sse", "streamable-http"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
WidgetTargetKind = Literal["widget-class", "factory", "async-factory"]
WidgetState = StateSpec | str | _DefaultState
_COMM_REPLAY_LIMIT = 128


class AppCSP(TypedDict, total=False):
    connectDomains: list[str]
    resourceDomains: list[str]
    frameDomains: list[str]
    baseUriDomains: list[str]


class AppPermissions(TypedDict, total=False):
    camera: dict[str, Any]
    microphone: dict[str, Any]
    geolocation: dict[str, Any]
    clipboardWrite: dict[str, Any]


@dataclass(frozen=True)
class WidgetTargetDescription:
    target_name: str
    tool_name: str
    title: str
    description: str
    kind: WidgetTargetKind
    signature: inspect.Signature
    input_schema: dict[str, Any]


@dataclass
class _CommReplay:
    fingerprint: str
    result: CallToolResult | None
    error: str | None


@dataclass
class _PollReplay:
    operation_id: str
    result: CallToolResult


@dataclass
class _SessionLease:
    session: WidgetSession
    tool_name: str
    deadline: float
    active_calls: int = 0
    comm_replays: OrderedDict[str, _CommReplay] = field(default_factory=OrderedDict)
    last_poll_replay: _PollReplay | None = None
    protocol_lock: threading.RLock = field(default_factory=threading.RLock)


class _MCPHeadMiddleware:
    def __init__(self, app: ASGIApp, path: str) -> None:
        self._app = app
        self._path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "HEAD"
            and scope["path"] == self._path
        ):
            response = Response(
                status_code=200,
                headers={"Allow": "GET, POST, DELETE, HEAD"},
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class WidgetTools:
    """Register AnyWidget tools on a FastMCP server and own their sessions."""

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
        self._mcp = mcp
        self._app_uri = app_uri
        self._csp = _app_csp(csp)
        self._permissions = permissions
        self._prefers_border = prefers_border
        self._session_idle_timeout = float(session_idle_timeout)
        self._sessions: dict[str, _SessionLease] = {}
        self._sessions_lock = threading.RLock()
        self._expiry_condition = threading.Condition(self._sessions_lock)
        self._expiry_thread: threading.Thread | None = None
        self._expiry_stopped = False
        self._register_app_resource()
        self._register_session_tools()

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
    ) -> TargetT | Callable[[TargetT], TargetT]:
        """Register an AnyWidget class or factory as an MCP App tool.

        Args:
            target: An ``AnyWidget`` subclass or a callable that creates a fresh
                widget. Omit it to use this method as a decorator.
            name: Tool name. Widget classes default to their snake-case class name.
            title: Human-facing tool title. Defaults to a title derived from the
                tool name.
            description: Tool description. Defaults to the target docstring.
            state: Model-visible state. Omit it for synchronized root traits, pass
                one trait name, a tuple of trait names, a projection callable, or
                ``None`` to disable model-context projection. Projection callables
                must not mutate synchronized widget traits.

        Returns:
            The registered target unchanged, or a decorator when ``target`` is
            omitted.

        Raises:
            TypeError: If the target, signature, or state option is invalid.
            ValueError: If the tool name is reserved or already registered.
            ToolError: If a tool invocation cannot create or open its widget.

        The target signature becomes the MCP input schema. A factory is called
        once per invocation and may be synchronous or asynchronous.

        Example:
            ``widgets.widget(ColorPicker, state="color")``
        """
        normalized_state = _normalize_state(state)
        validate_state_spec(normalized_state)

        def register(candidate: TargetT) -> TargetT:
            target_description = describe_widget_target(
                candidate,
                name=name,
                title=title,
                description=description,
            )
            tool_name = target_description.tool_name
            tool_title = target_description.title
            if self._mcp._tool_manager.get_tool(tool_name) is not None:
                raise ValueError(f"Tool name {tool_name!r} is already registered")

            @functools.wraps(candidate)
            async def launch(**arguments: Any) -> CallToolResult:
                created = candidate(**arguments)
                widget = await created if inspect.isawaitable(created) else created
                if not isinstance(widget, AnyWidget):
                    raise ToolError(
                        f"Widget factory returned {type(widget).__name__}, expected AnyWidget"
                    )
                return self._open_widget(
                    widget,
                    normalized_state,
                    tool_name=tool_name,
                    tool_title=tool_title,
                )

            setattr(
                launch,
                "__signature__",
                target_description.signature.replace(return_annotation=CallToolResult),
            )
            tool_meta = {"ui": {"resourceUri": self._app_uri}}
            self._mcp.add_tool(
                launch,
                name=tool_name,
                title=tool_title,
                description=target_description.description,
                meta=tool_meta,
                structured_output=False,
            )
            registered = self._mcp._tool_manager.get_tool(tool_name)
            assert registered is not None
            assert registered.parameters == target_description.input_schema
            return candidate

        if target is None:
            return register
        return register(target)

    def close(self) -> None:
        """Close every live widget session owned by this adapter."""
        with self._expiry_condition:
            leases = list(self._sessions.values())
            self._sessions.clear()
            self._expiry_stopped = True
            expiry_thread = self._expiry_thread
            self._expiry_condition.notify_all()
        for lease in leases:
            _close_session(lease.session, "server shutdown")
        if (
            expiry_thread is not None
            and expiry_thread is not threading.current_thread()
        ):
            expiry_thread.join()

    def _open_widget(
        self,
        widget: AnyWidget,
        state: StateSpec | _DefaultState,
        *,
        tool_name: str,
        tool_title: str,
    ) -> CallToolResult:
        instance_id = uuid.uuid4().hex
        try:
            session = WidgetSession(instance_id, widget, state)
        except WidgetInUseError as error:
            raise ToolError(str(error)) from error
        except (TypeError, ValueError) as error:
            raise ToolError(str(error)) from error
        except Exception:
            try:
                widget.close()
            except Exception:
                logger.exception("Failed to close widget after launch failure")
            raise

        try:
            root_model_id = session.root_model_id
            launch = session.launch_snapshot()
            models = launch.models
            if launch.projection_error is not None:
                raise ToolError(
                    f"Widget state projection failed: {launch.projection_error}"
                )
            projection = launch.projection
        except TimeoutError as error:
            _close_session(session, "launch snapshot timeout")
            raise ToolError(f"Widget launch snapshot failed: {error}") from error
        except Exception:
            _close_session(session, "widget launch failure")
            raise

        closed = False
        with self._expiry_condition:
            if self._expiry_stopped:
                closed = True
            else:
                lease = _SessionLease(
                    session=session,
                    tool_name=tool_name,
                    deadline=time.monotonic() + self._session_idle_timeout,
                )
                self._sessions[instance_id] = lease
                self._start_expiry_thread()
                self._expiry_condition.notify()
        if closed:
            _close_session(session, "closed server launch")
            raise ToolError("The AnyWidget MCP server is closed")

        structured: dict[str, Any] = {"tool": tool_name}
        if projection is None:
            text = f"Opened {tool_title}."
        else:
            structured["state"] = projection.state
            text = (
                f"Opened {tool_title} with state {_canonical_json(projection.state)}."
            )

        runtime: dict[str, Any] = {
            "instanceId": instance_id,
            "rootModelId": root_model_id,
            "models": models,
            "messages": launch.messages,
        }
        if projection is not None:
            runtime["context"] = _context_payload(tool_name, projection)
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=structured,
            _meta={
                "ui": {"resourceUri": self._app_uri},
                "anywidget": runtime,
            },
        )

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
        app_only = {"ui": {"visibility": ["app"]}}

        async def anywidget_comm(
            instance_id: str,
            model_id: str,
            data: dict[str, Any],
            operation_id: str,
            buffers: list[str] | None = None,
        ) -> CallToolResult:
            with self._use_lease(instance_id) as lease:
                with lease.protocol_lock:
                    fingerprint = _comm_fingerprint(model_id, data, buffers or ())
                    _validate_operation_id(operation_id)
                    replay = lease.comm_replays.get(operation_id)
                    if replay is not None:
                        if replay.fingerprint != fingerprint:
                            raise ToolError(
                                f"operation_id {operation_id!r} was reused for another comm request"
                            )
                        lease.comm_replays.move_to_end(operation_id)
                        if replay.error is not None:
                            raise ToolError(replay.error)
                        assert replay.result is not None
                        return replay.result.model_copy(deep=True)

                    try:
                        snapshot = lease.session.receive(model_id, data, buffers or ())
                        result = _session_result(lease, snapshot)
                    except Exception as error:
                        message = str(error)
                        _remember_comm_replay(
                            lease,
                            operation_id,
                            _CommReplay(
                                fingerprint=fingerprint,
                                result=None,
                                error=message,
                            ),
                        )
                        raise ToolError(message) from error
                    _remember_comm_replay(
                        lease,
                        operation_id,
                        _CommReplay(
                            fingerprint=fingerprint,
                            result=result.model_copy(deep=True),
                            error=None,
                        ),
                    )
                    return result

        async def anywidget_poll(
            instance_id: str,
            operation_id: str,
        ) -> CallToolResult:
            with self._use_lease(instance_id) as lease:
                with lease.protocol_lock:
                    _validate_operation_id(operation_id)
                    replay = lease.last_poll_replay
                    if replay is not None and replay.operation_id == operation_id:
                        return replay.result.model_copy(deep=True)

                    result = _session_result(lease, lease.session.snapshot())
                    lease.last_poll_replay = _PollReplay(
                        operation_id=operation_id,
                        result=result.model_copy(deep=True),
                    )
                    return result

        async def anywidget_dispose(instance_id: str) -> CallToolResult:
            with self._expiry_condition:
                lease = self._sessions.pop(instance_id, None)
                self._expiry_condition.notify()
            if lease is not None:
                _close_session(lease.session, "app disposal")
            return CallToolResult(
                content=[],
                structuredContent={"disposed": lease is not None},
            )

        self._mcp.add_tool(anywidget_comm, meta=app_only, structured_output=False)
        self._mcp.add_tool(anywidget_poll, meta=app_only, structured_output=False)
        self._mcp.add_tool(anywidget_dispose, meta=app_only, structured_output=False)

    @contextmanager
    def _use_lease(self, instance_id: str) -> Generator[_SessionLease, None, None]:
        with self._expiry_condition:
            lease = self._sessions.get(instance_id)
            if lease is not None:
                lease.active_calls += 1
                lease.deadline = time.monotonic() + self._session_idle_timeout
                self._expiry_condition.notify()
        if lease is None:
            raise ToolError(f"Unknown widget session: {instance_id}")
        try:
            yield lease
        finally:
            with self._expiry_condition:
                lease.active_calls -= 1
                if self._sessions.get(instance_id) is lease:
                    lease.deadline = time.monotonic() + self._session_idle_timeout
                self._expiry_condition.notify()

    def _start_expiry_thread(self) -> None:
        if self._expiry_thread is not None:
            return
        self._expiry_thread = threading.Thread(
            target=self._expire_sessions,
            name=f"anywidget-mcp-expiry-{id(self):x}",
            daemon=True,
        )
        self._expiry_thread.start()

    def _expire_sessions(self) -> None:
        while True:
            expired: list[_SessionLease] = []
            with self._expiry_condition:
                while not self._expiry_stopped:
                    idle_leases = {
                        instance_id: lease
                        for instance_id, lease in self._sessions.items()
                        if lease.active_calls == 0
                    }
                    if not idle_leases:
                        self._expiry_condition.wait()
                        continue

                    now = time.monotonic()
                    deadline = min(lease.deadline for lease in idle_leases.values())
                    remaining = deadline - now
                    if remaining > 0:
                        self._expiry_condition.wait(remaining)
                        continue

                    expired_ids = [
                        instance_id
                        for instance_id, lease in idle_leases.items()
                        if lease.deadline <= now
                    ]
                    expired = [
                        self._sessions.pop(instance_id) for instance_id in expired_ids
                    ]
                    break

                if self._expiry_stopped:
                    return

            for lease in expired:
                _close_session(lease.session, "idle expiry")


def attach(
    mcp: FastMCP,
    *,
    app_uri: str = APP_RESOURCE_URI,
    csp: AppCSP | None = None,
    permissions: AppPermissions | None = None,
    prefers_border: bool = True,
    session_idle_timeout: float = 900.0,
) -> WidgetTools:
    """Attach AnyWidget registration and session handling to ``mcp``.

    Call :meth:`WidgetTools.close` when the embedding server stops.
    """
    if not isinstance(mcp, FastMCP):
        raise TypeError("attach() expected a FastMCP server")
    if getattr(mcp, "_anywidget_tools", None) is not None:
        raise ValueError("AnyWidget tools are already attached to this FastMCP server")
    conflicts = [
        name for name in _SESSION_TOOL_NAMES if mcp._tool_manager.get_tool(name)
    ]
    if conflicts:
        joined = ", ".join(sorted(conflicts))
        raise ValueError(f"FastMCP already defines reserved AnyWidget tools: {joined}")

    tools = WidgetTools(
        mcp,
        app_uri=app_uri,
        csp=csp,
        permissions=permissions,
        prefers_border=prefers_border,
        session_idle_timeout=session_idle_timeout,
    )
    setattr(mcp, "_anywidget_tools", tools)
    return tools


class AnyWidgetMCP(FastMCP):
    """Run a FastMCP server that owns its AnyWidget session adapter."""

    def __init__(
        self,
        name: str | None = None,
        *,
        app_uri: str = APP_RESOURCE_URI,
        csp: AppCSP | None = None,
        permissions: AppPermissions | None = None,
        prefers_border: bool = True,
        cors_origins: Sequence[str] = (),
        session_idle_timeout: float = 900.0,
        **fastmcp_options: Any,
    ) -> None:
        super().__init__(name, **fastmcp_options)
        self._cors_origins = tuple(cors_origins)
        self._widget_tools = attach(
            self,
            app_uri=app_uri,
            csp=csp,
            permissions=permissions,
            prefers_border=prefers_border,
            session_idle_timeout=session_idle_timeout,
        )

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
    ) -> TargetT | Callable[[TargetT], TargetT]:
        """Register an AnyWidget class or factory as an MCP App tool."""
        return self._widget_tools.widget(
            target,
            name=name,
            title=title,
            description=description,
            state=state,
        )

    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        app.add_middleware(
            _MCPHeadMiddleware,
            path=self.settings.streamable_http_path,
        )
        if self._cors_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=list(self._cors_origins),
                allow_methods=["GET", "POST", "DELETE", "HEAD"],
                allow_headers=["*"],
                expose_headers=["Mcp-Session-Id"],
            )
        return app

    def run(
        self,
        transport: RunTransport = "stdio",
        mount_path: str | None = None,
    ) -> None:
        """Run the owned server and close its widget sessions on exit."""
        try:
            super().run(transport=transport, mount_path=mount_path)
        finally:
            self.close()

    def close(self) -> None:
        """Close every live widget session owned by this server."""
        self._widget_tools.close()


def serve(
    target: WidgetTarget,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    state: WidgetState = DEFAULT_STATE,
    transport: Transport = "streamable-http",
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: LogLevel = "INFO",
    **fastmcp_options: Any,
) -> None:
    """Register one widget and run its MCP server until the transport exits.

    Args:
        target: An ``AnyWidget`` subclass or factory registered as the sole widget
            tool.
        name: Tool name forwarded to :meth:`AnyWidgetMCP.widget`.
        title: Human-facing tool title.
        description: Tool description.
        state: Model-visible state selection or a read-only projection callable.
        transport: ``"streamable-http"`` or ``"stdio"``.
        host: Streamable HTTP bind address.
        port: Streamable HTTP bind port.
        log_level: Server log level.
        **fastmcp_options: Additional options forwarded to :class:`AnyWidgetMCP`.

    Raises:
        TypeError: If the target, signature, or state option is invalid.
        ValueError: If the tool name is reserved or already registered.

    Registration errors propagate before the transport starts. Widget sessions
    are closed when the transport exits or raises.

    Example:
        ``serve(ColorPicker, transport="stdio")``
    """
    target_name = getattr(target, "__name__", type(target).__name__)
    server = AnyWidgetMCP(
        f"{target_name} MCP",
        host=host,
        port=port,
        log_level=log_level,
        **fastmcp_options,
    )
    try:
        server.widget(
            target,
            name=name,
            title=title,
            description=description,
            state=state,
        )
        server.run(transport=transport)
    finally:
        server.close()


def _session_result(
    lease: _SessionLease,
    snapshot: SessionSnapshot,
) -> CallToolResult:
    payload: dict[str, Any] = {"messages": snapshot.messages}
    if snapshot.models:
        payload["models"] = snapshot.models
    if snapshot.removed_model_ids:
        payload["removedModelIds"] = snapshot.removed_model_ids
    if snapshot.projection_error is not None:
        payload["contextError"] = (
            f"Widget state projection failed: {snapshot.projection_error}"
        )
    elif snapshot.projection is not None:
        payload["context"] = _context_payload(
            lease.tool_name,
            snapshot.projection,
        )
    return CallToolResult(
        content=[],
        _meta={"anywidget": payload},
    )


def _comm_fingerprint(
    model_id: str,
    data: dict[str, Any],
    buffers: Sequence[str],
) -> str:
    return _canonical_json(
        {
            "modelId": model_id,
            "data": data,
            "buffers": list(buffers),
        }
    )


def _validate_operation_id(operation_id: str) -> None:
    if not operation_id or len(operation_id) > 128:
        raise ToolError("operation_id must contain between 1 and 128 characters")


def _remember_comm_replay(
    lease: _SessionLease,
    operation_id: str,
    replay: _CommReplay,
) -> None:
    lease.comm_replays[operation_id] = replay
    if len(lease.comm_replays) > _COMM_REPLAY_LIMIT:
        lease.comm_replays.popitem(last=False)


def _close_session(session: WidgetSession, reason: str) -> None:
    try:
        session.close()
    except Exception:
        logger.exception("Failed to close widget session during %s", reason)


def _context_payload(
    tool_name: str,
    projection: ProjectionUpdate,
) -> dict[str, Any]:
    return {
        "version": projection.version,
        "tool": tool_name,
        "state": projection.state,
    }


def describe_widget_target(
    candidate: object,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> WidgetTargetDescription:
    """Validate a widget target and return its MCP tool description."""
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
    if tool_name in _SESSION_TOOL_NAMES:
        raise ValueError(
            f"Widget tool name {tool_name!r} is reserved for the AnyWidget app"
        )
    tool_title = title if title is not None else _tool_title(tool_name)
    tool_description = (
        description if description is not None else (_direct_doc(target) or "")
    )
    signature = _tool_signature(target, is_widget_class=is_widget_class)
    if is_widget_class:
        kind: WidgetTargetKind = "widget-class"
    elif _is_async_callable(target):
        kind = "async-factory"
    else:
        kind = "factory"
    input_schema = _input_schema(target_name, signature)
    return WidgetTargetDescription(
        target_name=target_name,
        tool_name=tool_name,
        title=tool_title,
        description=tool_description,
        kind=kind,
        signature=signature,
        input_schema=input_schema,
    )


def _normalize_state(state: WidgetState) -> StateSpec | _DefaultState:
    return (state,) if isinstance(state, str) else state


def _input_schema(
    target_name: str,
    signature: inspect.Signature,
) -> dict[str, Any]:
    def schema_target(**_arguments: Any) -> CallToolResult:
        raise RuntimeError("Widget schema targets are not executable")

    schema_target.__name__ = target_name
    setattr(
        schema_target,
        "__signature__",
        signature.replace(return_annotation=CallToolResult),
    )
    return Tool.from_function(
        schema_target,
        structured_output=False,
    ).parameters


def _is_async_callable(candidate: Callable[..., Any]) -> bool:
    while isinstance(candidate, functools.partial):
        candidate = candidate.func
    return inspect.iscoroutinefunction(candidate) or inspect.iscoroutinefunction(
        getattr(candidate, "__call__", None)
    )


def _tool_signature(
    candidate: Callable[..., Any],
    *,
    is_widget_class: bool,
) -> inspect.Signature:
    signature = inspect.signature(candidate)
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
        parameters.append(parameter)
    return signature.replace(parameters=parameters)


def _snake_case(name: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    value = re.sub(r"([a-z])([A-Z])", r"\1_\2", value)
    value = re.sub(r"([A-Za-z])([0-9]+)", r"\1_\2", value)
    return value.replace("-", "_").lower()


def _direct_doc(candidate: Callable[..., Any]) -> str | None:
    doc = getattr(candidate, "__doc__", None)
    return inspect.cleandoc(doc) if doc else None


def _tool_title(name: str) -> str:
    return _snake_case(name).replace("_", " ").title()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _app_csp(csp: AppCSP | None) -> AppCSP:
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
