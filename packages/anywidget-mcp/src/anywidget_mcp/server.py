from __future__ import annotations

import functools
import inspect
import json
import logging
import math
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
)
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, Literal, TypeVar, TypedDict, cast, get_type_hints, overload

import anyio
from anywidget import AnyWidget
from anyio.abc import TaskGroup
from anyio.lowlevel import checkpoint
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools import Tool
from mcp.types import CallToolResult, Icon, TextContent, ToolAnnotations
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ._bridge import (
    PROTOCOL_VERSION,
    SessionSnapshot,
    WidgetInUseError,
    WidgetSession,
    WidgetSessionInitializationError,
)
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
    {"anywidget_assets", "anywidget_comm", "anywidget_poll", "anywidget_dispose"}
)

WidgetFactoryResult = (
    AnyWidget
    | AbstractContextManager[AnyWidget]
    | AbstractAsyncContextManager[AnyWidget]
)
WidgetFactory = Callable[..., WidgetFactoryResult | Awaitable[WidgetFactoryResult]]
WidgetTarget = type[AnyWidget] | WidgetFactory
TargetT = TypeVar("TargetT", bound=Callable[..., Any])
Transport = Literal["streamable-http", "stdio"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
WidgetTargetKind = Literal["widget-class", "factory"]
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


@dataclass(frozen=True)
class _CompiledWidgetTarget:
    description: WidgetTargetDescription
    tool: Tool
    invocation_adapter: Callable[..., Awaitable[CallToolResult]] = field(
        repr=False,
        compare=False,
    )
    _bind_invocation: Callable[
        [Callable[[dict[str, Any]], Awaitable[CallToolResult]]],
        None,
    ] = field(repr=False, compare=False)

    def bind(
        self,
        invoke: Callable[[dict[str, Any]], Awaitable[CallToolResult]],
    ) -> Callable[..., Awaitable[CallToolResult]]:
        """Bind the compiled public signature to one runtime invocation."""
        self._bind_invocation(invoke)
        return self.invocation_adapter


@dataclass
class _CommReplay:
    fingerprint: str
    result: CallToolResult | None
    error: str | None
    asset_ids: tuple[str, ...] = ()


@dataclass
class _PollReplay:
    operation_id: str
    acknowledged_model_ids: tuple[str, ...]
    result: CallToolResult
    asset_ids: tuple[str, ...] = ()


class _FactoryOwner:
    def __init__(self) -> None:
        self.ready = anyio.Event()
        self.close_requested = anyio.Event()
        self.closed = anyio.Event()
        self.widget: AnyWidget | None = None
        self.session: WidgetSession | None = None
        self.error: BaseException | None = None
        self.cleanup_error: BaseException | None = None
        self._acquisition_scope: anyio.CancelScope | None = None
        self._close_unowned_widget = True
        self._close_reason = "session cleanup"

    async def run(
        self,
        candidate: Callable[..., Any],
        arguments: dict[str, Any],
    ) -> None:
        try:
            with anyio.CancelScope(shield=True):
                # Manager exits stay outside the cancellable acquisition scope so
                # post-yield cleanup can await during request cancellation.
                async with AsyncExitStack() as stack:
                    if self.close_requested.is_set():
                        arguments.clear()
                        return
                    widget: object | None = None
                    with anyio.CancelScope() as acquisition_scope:
                        self._acquisition_scope = acquisition_scope
                        try:
                            if self.close_requested.is_set():
                                arguments.clear()
                                return
                            try:
                                created = candidate(**arguments)
                            finally:
                                arguments.clear()
                            try:
                                value = (
                                    await created
                                    if inspect.isawaitable(created)
                                    else created
                                )
                            finally:
                                del created
                            if isinstance(value, AbstractAsyncContextManager):
                                widget = await stack.enter_async_context(value)
                            elif isinstance(value, AbstractContextManager):
                                widget = stack.enter_context(value)
                            elif isinstance(value, AnyWidget):
                                widget = value
                            else:
                                raise ToolError(
                                    "Widget factory returned "
                                    f"{type(value).__name__}, expected AnyWidget "
                                    "or a context manager"
                                )
                        finally:
                            self._acquisition_scope = None
                    if acquisition_scope.cancel_called:
                        return
                    await self._hold(widget)
        except BaseException as error:
            self.error = error
        finally:
            self.ready.set()
            self.closed.set()

    async def wait_ready(self) -> AnyWidget:
        await self.ready.wait()
        if self.widget is not None:
            return self.widget
        if self.error is not None:
            raise self.error
        raise RuntimeError("Widget factory closed before yielding a widget")

    def assign_session(self, session: WidgetSession) -> None:
        self.session = session

    def leave_unowned_widget_open(self) -> None:
        """Keep a widget owned by another live session open during cleanup."""
        self._close_unowned_widget = False

    def request_close(self, reason: str) -> None:
        if not self.close_requested.is_set():
            self._close_reason = reason
            self.close_requested.set()
        if not self.ready.is_set() and self._acquisition_scope is not None:
            self._acquisition_scope.cancel()

    async def wait_closed(self) -> BaseException | None:
        await self.closed.wait()
        return self.error

    async def _hold(self, widget: object) -> None:
        if not isinstance(widget, AnyWidget):
            raise ToolError(
                "Widget factory context manager yielded "
                f"{type(widget).__name__}, expected AnyWidget"
            )
        self.widget = widget
        self.ready.set()
        try:
            await self.close_requested.wait()
        finally:
            self._cleanup_once()

    def retry_cleanup(self) -> BaseException | None:
        if self.cleanup_error is not None:
            self._cleanup_once()
        return self.cleanup_error

    def _cleanup_once(self) -> None:
        try:
            if self.session is not None:
                self.session.close()
                self.session = None
                self.widget = None
            elif self._close_unowned_widget and self.widget is not None:
                self.widget.close()
                self.widget = None
        except BaseException as error:
            self.cleanup_error = error
        else:
            self.cleanup_error = None


@dataclass
class _SessionLease:
    session: WidgetSession
    owner: _FactoryOwner
    tool_name: str
    deadline: float
    active_calls: int = 0
    comm_replays: OrderedDict[str, _CommReplay] = field(default_factory=OrderedDict)
    last_poll_replay: _PollReplay | None = None
    protocol_lock: threading.RLock = field(default_factory=threading.RLock)
    idle: anyio.Event = field(default_factory=anyio.Event)
    expiry_scope: anyio.CancelScope | None = None

    def __post_init__(self) -> None:
        self.idle.set()


class _SessionRuntime:
    def __init__(
        self,
        task_group: TaskGroup,
        *,
        session_idle_timeout: float,
        app_uri: str,
    ) -> None:
        self._task_group = task_group
        self._session_idle_timeout = session_idle_timeout
        self._app_uri = app_uri
        self._sessions: dict[str, _SessionLease] = {}
        self._owners: set[_FactoryOwner] = set()
        self._owned_leases: dict[_FactoryOwner, _SessionLease] = {}
        self._lock = threading.RLock()
        self._close_lock = anyio.Lock()
        self._accepting = True

    async def open(
        self,
        candidate: Callable[..., Any],
        arguments: dict[str, Any],
        state: StateSpec | _DefaultState,
        *,
        tool_name: str,
        tool_title: str,
    ) -> CallToolResult:
        owner = _FactoryOwner()
        lease: _SessionLease | None = None
        instance_id: str | None = None
        with self._lock:
            if not self._accepting:
                raise ToolError("The AnyWidget MCP server runtime is closing")
            self._owners.add(owner)
        self._task_group.start_soon(owner.run, candidate, arguments)

        try:
            widget = await owner.wait_ready()
            with self._lock:
                if not self._accepting:
                    raise ToolError("The AnyWidget MCP server runtime is closing")

            instance_id = uuid.uuid4().hex
            try:
                session = WidgetSession(instance_id, widget, state)
            except WidgetInUseError as error:
                owner.leave_unowned_widget_open()
                raise ToolError(str(error)) from error
            except WidgetSessionInitializationError as error:
                owner.assign_session(error.session)
                raise
            except (TypeError, ValueError) as error:
                owner.leave_unowned_widget_open()
                raise ToolError(str(error)) from error
            except Exception:
                owner.leave_unowned_widget_open()
                raise
            owner.assign_session(session)

            try:
                launch = session.launch_snapshot()
                if launch.projection_error is not None:
                    raise ToolError(
                        f"Widget state projection failed: {launch.projection_error}"
                    )
            except TimeoutError as error:
                raise ToolError(f"Widget launch snapshot failed: {error}") from error

            # The first checkpoint schedules overdue request-cancellation timers.
            # The second observes the canceled scope before the session is committed.
            await checkpoint()
            await checkpoint()

            lease = _SessionLease(
                session=session,
                owner=owner,
                tool_name=tool_name,
                deadline=time.monotonic() + self._session_idle_timeout,
            )
            with self._lock:
                if not self._accepting:
                    raise ToolError("The AnyWidget MCP server runtime is closing")
                self._sessions[instance_id] = lease
                self._owned_leases[owner] = lease
            self._task_group.start_soon(self._expire, instance_id, lease)
            return _launch_result(
                lease,
                launch,
                tool_title=tool_title,
                app_uri=self._app_uri,
            )
        except BaseException as error:
            with anyio.CancelScope(shield=True):
                taken: _SessionLease | None = None
                if lease is not None and instance_id is not None:
                    taken = self._take(instance_id, lease)
                try:
                    if taken is not None:
                        await self._finish_close(taken, "widget launch failure")
                    else:
                        await self._finish_owner_close(
                            owner,
                            "widget launch failure",
                            observed_error=error,
                        )
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup(
                        "Widget launch and cleanup failed",
                        [error, cleanup_error],
                    ) from error
            raise

    @contextmanager
    def use(self, instance_id: str) -> Generator[_SessionLease, None, None]:
        with self._lock:
            lease = self._sessions.get(instance_id)
            if lease is not None:
                if lease.active_calls == 0:
                    lease.idle = anyio.Event()
                lease.active_calls += 1
                lease.deadline = time.monotonic() + self._session_idle_timeout
        if lease is None:
            raise ToolError(f"Unknown widget session: {instance_id}")
        try:
            yield lease
        finally:
            with self._lock:
                lease.active_calls -= 1
                if self._sessions.get(instance_id) is lease:
                    lease.deadline = time.monotonic() + self._session_idle_timeout
                if lease.active_calls == 0:
                    lease.idle.set()

    async def dispose(self, instance_id: str, reason: str) -> bool:
        lease = self._take(instance_id)
        if lease is None:
            return False
        with anyio.CancelScope(shield=True):
            await self._finish_close(lease, reason)
        return True

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            async with self._close_lock:
                with self._lock:
                    self._accepting = False
                    leases = list(self._sessions.values())
                    self._sessions.clear()
                    owners = set(self._owners)
                    active_owners = {lease.owner for lease in leases}
                    closing_owners = set(self._owned_leases) - active_owners
                    pending_owners = owners - set(self._owned_leases)
                    for lease in leases:
                        self._cancel_expiry(lease)

                cleanup_errors: list[BaseException] = []
                for lease in leases:
                    try:
                        await self._finish_close(lease, "server shutdown")
                    except BaseException as error:
                        cleanup_errors.append(error)

                for owner in closing_owners:
                    lease = self._owned_leases.get(owner)
                    if lease is None:
                        continue
                    try:
                        await self._finish_close(lease, "server shutdown")
                    except BaseException as error:
                        cleanup_errors.append(error)

                for owner in pending_owners:
                    try:
                        await self._finish_owner_close(owner, "server shutdown")
                    except BaseException as error:
                        cleanup_errors.append(error)

                if cleanup_errors:
                    raise BaseExceptionGroup(
                        "Failed to close widget server runtime",
                        cleanup_errors,
                    )

    def _take(
        self,
        instance_id: str,
        expected: _SessionLease | None = None,
    ) -> _SessionLease | None:
        with self._lock:
            lease = self._sessions.get(instance_id)
            if lease is None or (expected is not None and lease is not expected):
                return None
            del self._sessions[instance_id]
            self._cancel_expiry(lease)
            return lease

    async def _finish_close(self, lease: _SessionLease, reason: str) -> None:
        with anyio.CancelScope(shield=True):
            await lease.idle.wait()
            await self._finish_owner_close(lease.owner, reason)

    async def _finish_owner_close(
        self,
        owner: _FactoryOwner,
        reason: str,
        *,
        observed_error: BaseException | None = None,
    ) -> None:
        owner.request_close(reason)
        owner_error = await owner.wait_closed()
        cleanup_error = owner.retry_cleanup()
        errors = [
            error
            for error in (owner_error, cleanup_error)
            if error is not None and error is not observed_error
        ]
        if cleanup_error is None:
            self._forget_owner(owner)
        if errors:
            raise BaseExceptionGroup(
                f"Widget factory cleanup failed during {reason}",
                errors,
            )

    def _forget_owner(self, owner: _FactoryOwner) -> None:
        with self._lock:
            self._owners.discard(owner)
            self._owned_leases.pop(owner, None)

    async def _expire(self, instance_id: str, lease: _SessionLease) -> None:
        expired = False
        with anyio.CancelScope() as cancel_scope:
            lease.expiry_scope = cancel_scope
            while True:
                with self._lock:
                    if self._sessions.get(instance_id) is not lease:
                        return
                    active = lease.active_calls
                    idle = lease.idle
                    remaining = lease.deadline - time.monotonic()
                if active:
                    await idle.wait()
                    continue
                if remaining > 0:
                    await anyio.sleep(remaining)
                    continue
                with self._lock:
                    if self._sessions.get(instance_id) is not lease:
                        return
                    if lease.active_calls or lease.deadline > time.monotonic():
                        continue
                    del self._sessions[instance_id]
                    lease.expiry_scope = None
                    expired = True
                break
        if expired:
            try:
                await self._finish_close(lease, "idle expiry")
            except BaseException as error:
                logger.error("Widget cleanup failed during idle expiry: %s", error)

    @staticmethod
    def _cancel_expiry(lease: _SessionLease) -> None:
        scope = lease.expiry_scope
        lease.expiry_scope = None
        if scope is not None:
            scope.cancel()


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
        app_uri = _validated_app_uri(mcp, app_uri)
        conflicts = [
            name for name in _SESSION_TOOL_NAMES if mcp._tool_manager.get_tool(name)
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
        self._runtime: _SessionRuntime | None = None
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
                widget. Omit it to use this method as a decorator.
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

        The target signature becomes the MCP input schema. FastMCP injects a
        ``Context`` parameter without exposing it in that schema. A factory may
        return a widget directly or through a synchronous or asynchronous context
        manager. The context manager remains active for the widget session. Factory
        acquisition is cancellable, so the manager owns rollback for resources
        acquired before it yields.

        Example:
            ``widgets.widget(ColorPicker, state="color")``
        """
        normalized_state = _normalize_state(state)
        validate_state_spec(normalized_state)

        def register(candidate: TargetT) -> TargetT:
            tool_meta = {"ui": {"resourceUri": self._app_uri}}
            compiled = _compile_widget_target(
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

            async def invoke(arguments: dict[str, Any]) -> CallToolResult:
                return await self._require_runtime().open(
                    candidate,
                    arguments,
                    normalized_state,
                    tool_name=tool_name,
                    tool_title=tool_title,
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

    def _require_runtime(self) -> _SessionRuntime:
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

    @asynccontextmanager
    async def _runtime_lifespan(self):
        async with anyio.create_task_group() as task_group:
            runtime = _SessionRuntime(
                task_group,
                session_idle_timeout=self._session_idle_timeout,
                app_uri=self._app_uri,
            )
            with self._runtime_lock:
                if self._runtime is not None:
                    raise RuntimeError(
                        "The AnyWidget MCP server runtime is already active"
                    )
                self._runtime = runtime
            try:
                yield
            finally:
                with anyio.CancelScope(shield=True):
                    try:
                        await runtime.aclose()
                    finally:
                        with self._runtime_lock:
                            if self._runtime is runtime:
                                self._runtime = None
                        task_group.cancel_scope.cancel()

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
            with self._require_runtime().use(instance_id) as lease:
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
                            asset_ids=tuple(snapshot.asset_manifest),
                        ),
                    )
                    return result

        async def anywidget_poll(
            instance_id: str,
            operation_id: str,
            acknowledged_model_ids: list[str] | None = None,
        ) -> CallToolResult:
            with self._require_runtime().use(instance_id) as lease:
                with lease.protocol_lock:
                    _validate_operation_id(operation_id)
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
                        return replay.result.model_copy(deep=True)

                    try:
                        lease.session.acknowledge_model_removals(acknowledgments)
                    except (KeyError, RuntimeError, ValueError) as error:
                        raise ToolError(str(error)) from error
                    snapshot = lease.session.snapshot()
                    result = _session_result(lease, snapshot)
                    replay = _PollReplay(
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
                    return result

        async def anywidget_dispose(instance_id: str) -> CallToolResult:
            disposed = await self._require_runtime().dispose(
                instance_id,
                "app disposal",
            )
            return CallToolResult(
                content=[],
                structuredContent={"disposed": disposed},
            )

        async def anywidget_assets(
            instance_id: str,
            asset_ids: list[str],
        ) -> CallToolResult:
            with self._require_runtime().use(instance_id) as lease:
                with lease.protocol_lock:
                    try:
                        contents = lease.session.asset_contents(asset_ids)
                    except (KeyError, RuntimeError, ValueError) as error:
                        raise ToolError(str(error)) from error
            return CallToolResult(
                content=[],
                _meta={
                    "anywidget": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "assetContents": contents,
                    }
                },
            )

        self._mcp.add_tool(anywidget_assets, meta=app_only, structured_output=False)
        self._mcp.add_tool(anywidget_comm, meta=app_only, structured_output=False)
        self._mcp.add_tool(anywidget_poll, meta=app_only, structured_output=False)
        self._mcp.add_tool(anywidget_dispose, meta=app_only, structured_output=False)


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

    The adapter composes session cleanup into the server lifespan. Await
    :meth:`WidgetTools.aclose` to close live sessions early.
    """
    if not isinstance(mcp, FastMCP):
        raise TypeError("attach() expected a FastMCP server")
    if getattr(mcp, "_anywidget_tools", None) is not None:
        raise ValueError("AnyWidget tools are already attached to this FastMCP server")
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
        """Register an AnyWidget class or factory as an MCP App tool."""
        return self._widget_tools.widget(
            target,
            name=name,
            title=title,
            description=description,
            state=state,
            annotations=annotations,
            icons=icons,
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

    async def aclose(self) -> None:
        """Close every live widget session in the active server lifespan."""
        await self._widget_tools.aclose()


def serve(
    target: WidgetTarget,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    state: WidgetState = DEFAULT_STATE,
    annotations: ToolAnnotations | None = None,
    icons: list[Icon] | None = None,
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
        annotations: Standard MCP tool behavior hints.
        icons: Icons applied to the sole widget tool and its MCP server.
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
        icons=icons,
        **fastmcp_options,
    )
    server.widget(
        target,
        name=name,
        title=title,
        description=description,
        state=state,
        annotations=annotations,
        icons=icons,
    )
    server.run(transport=transport)


def _launch_result(
    lease: _SessionLease,
    launch: SessionSnapshot,
    *,
    tool_title: str,
    app_uri: str,
) -> CallToolResult:
    projection = launch.projection
    structured: dict[str, Any] = {"tool": lease.tool_name}
    if projection is None:
        text = f"Opened {tool_title}."
    else:
        structured["state"] = projection.state
        text = f"Opened {tool_title} with state {_canonical_json(projection.state)}."

    runtime: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "instanceId": lease.session.instance_id,
        "rootModelId": lease.session.root_model_id,
        "assetManifest": launch.asset_manifest,
        "models": launch.models,
        "messages": launch.messages,
    }
    if projection is not None:
        runtime["context"] = _context_payload(lease.tool_name, projection)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=structured,
        _meta={
            "ui": {"resourceUri": app_uri},
            "anywidget": runtime,
        },
    )


def _session_result(
    lease: _SessionLease,
    snapshot: SessionSnapshot,
) -> CallToolResult:
    payload: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "assetManifest": snapshot.asset_manifest,
        "messages": snapshot.messages,
    }
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
    replay.asset_ids = lease.session.pin_assets(replay.asset_ids)
    lease.comm_replays[operation_id] = replay
    if len(lease.comm_replays) > _COMM_REPLAY_LIMIT:
        _, evicted = lease.comm_replays.popitem(last=False)
        lease.session.release_assets(evicted.asset_ids)


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
    return _compile_widget_target(
        candidate,
        name=name,
        title=title,
        description=description,
    ).description


def _compile_widget_target(
    candidate: object,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    annotations: ToolAnnotations | None = None,
    icons: list[Icon] | None = None,
    meta: dict[str, Any] | None = None,
) -> _CompiledWidgetTarget:
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
    else:
        kind = "factory"
    invocation_adapter, bind_invocation = _invocation_adapter(
        target,
        signature,
        target_name=target_name,
        description=tool_description,
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
    return _CompiledWidgetTarget(
        description=target_description,
        tool=tool,
        invocation_adapter=invocation_adapter,
        _bind_invocation=bind_invocation,
    )


def _normalize_state(state: WidgetState) -> StateSpec | _DefaultState:
    return (state,) if isinstance(state, str) else state


def _invocation_adapter(
    target: Callable[..., Any],
    signature: inspect.Signature,
    *,
    target_name: str,
    description: str,
) -> tuple[
    Callable[..., Awaitable[CallToolResult]],
    Callable[[Callable[[dict[str, Any]], Awaitable[CallToolResult]]], None],
]:
    bound: list[Callable[[dict[str, Any]], Awaitable[CallToolResult]] | None] = [None]

    @functools.wraps(target)
    async def launch(**arguments: Any) -> CallToolResult:
        invoke = bound[0]
        if invoke is None:
            raise RuntimeError("The compiled widget target is not registered")
        return await invoke(arguments)

    launch.__name__ = target_name
    launch.__doc__ = description
    compiled_signature = signature.replace(return_annotation=CallToolResult)
    launch.__annotations__ = {
        parameter.name: parameter.annotation
        for parameter in compiled_signature.parameters.values()
        if parameter.annotation is not inspect.Parameter.empty
    }
    launch.__annotations__["return"] = CallToolResult
    setattr(
        launch,
        "__signature__",
        compiled_signature,
    )

    def bind(
        invoke: Callable[[dict[str, Any]], Awaitable[CallToolResult]],
    ) -> None:
        if bound[0] is not None:
            raise RuntimeError("The compiled widget target is already registered")
        bound[0] = invoke

    return launch, bind


def _tool_signature(
    candidate: Callable[..., Any],
    *,
    is_widget_class: bool,
) -> inspect.Signature:
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
    return signature.replace(parameters=parameters)


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
    return _snake_case(name).replace("_", " ").title()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


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
