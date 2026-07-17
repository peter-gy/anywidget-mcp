"""Own widget session leases, capabilities, replay, expiry, and MCP payloads."""

from __future__ import annotations

import copy
import logging
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
from anyio.abc import TaskGroup
from anyio.lowlevel import checkpoint
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from ._bridge import (
    PROTOCOL_VERSION,
    SessionSnapshot,
    WidgetInUseError,
    WidgetSession,
    WidgetSessionInitializationError,
)
from ._factory import FactoryOwner
from ._group import _group_state
from ._projection_json import canonical_json
from ._state import ProjectionUpdate, StateSpec, _DefaultState

logger = logging.getLogger(__name__)

COMM_REPLAY_LIMIT = 128
BOOTSTRAP_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
BOOTSTRAP_MARKER_PREFIX = "urn:anywidget-mcp:bootstrap:"
UNCLAIMED_SESSION_TIMEOUT = 30.0


@dataclass
class CommReplay:
    """Cached comm outcome with source assets pinned for replay."""

    fingerprint: str
    result: CallToolResult | None
    error: str | None
    asset_ids: tuple[str, ...] = ()


@dataclass
class PollReplay:
    """Cached poll result keyed by operation and removal acknowledgments."""

    operation_id: str
    acknowledged_model_ids: tuple[str, ...]
    result: CallToolResult
    asset_ids: tuple[str, ...] = ()


@dataclass
class SessionLease:
    """Hold capabilities, replay state, and ownership for one live session."""

    session: WidgetSession
    owner: FactoryOwner
    tool_name: str
    bootstrap_id: str
    state_id: str | None
    deadline: float
    established: bool = False
    bootstrap_payload: dict[str, Any] | None = None
    bootstrap_operation_id: str | None = None
    bootstrap_replay: CallToolResult | None = None
    bootstrap_asset_ids: tuple[str, ...] = ()
    active_calls: int = 0
    comm_replays: OrderedDict[str, CommReplay] = field(default_factory=OrderedDict)
    last_poll_replay: PollReplay | None = None
    protocol_lock: threading.RLock = field(default_factory=threading.RLock)
    idle: anyio.Event = field(default_factory=anyio.Event)
    expiry_scope: anyio.CancelScope | None = None

    def __post_init__(self) -> None:
        self.idle.set()


class SessionRuntime:
    """Own widget sessions from factory acquisition through disposal.

    Protocol calls borrow leases while cleanup waits. Each lease serializes comm,
    poll, asset, and projection operations through its protocol lock.
    """

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
        self._sessions: dict[str, SessionLease] = {}
        self._bootstraps: dict[str, SessionLease] = {}
        self._state_handles: dict[str, SessionLease] = {}
        self._owners: set[FactoryOwner] = set()
        self._owned_leases: dict[FactoryOwner, SessionLease] = {}
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
        loading_message: str = "Initializing widget…",
    ) -> CallToolResult:
        """Acquire, initialize, and atomically publish a widget session.

        The initial graph and projection complete before capability handles become
        reachable. Failures attempt cleanup and report cleanup errors with the
        launch error.
        """

        owner = FactoryOwner()
        lease: SessionLease | None = None
        instance_id: str | None = None
        with self._lock:
            if not self._accepting:
                raise ToolError("The AnyWidget MCP server runtime is closing")
            self._owners.add(owner)
        self._task_group.start_soon(owner.run, candidate, arguments)

        try:
            output = await owner.wait_ready()
            with self._lock:
                if not self._accepting:
                    raise ToolError("The AnyWidget MCP server runtime is closing")

            instance_id = uuid.uuid4().hex
            bootstrap_id = secrets.token_hex(16)
            while bootstrap_id == instance_id:
                bootstrap_id = secrets.token_hex(16)
            try:
                session_state = (
                    _group_state(state, output.state_roots)
                    if output.sequence
                    else state
                )
            except (TypeError, ValueError) as error:
                raise ToolError(str(error)) from error
            try:
                session = WidgetSession(instance_id, output.render_root, session_state)
            except WidgetInUseError as error:
                owner.leave_unowned_output_open()
                raise ToolError(str(error)) from error
            except WidgetSessionInitializationError as error:
                owner.assign_session(error.session)
                raise
            except (TypeError, ValueError) as error:
                owner.leave_unowned_output_open()
                raise ToolError(str(error)) from error
            except Exception:
                owner.leave_unowned_output_open()
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

            # Request cancellation must be observed before the session is committed.
            await checkpoint()
            await checkpoint()

            with self._lock:
                if not self._accepting:
                    raise ToolError("The AnyWidget MCP server runtime is closing")
                state_id = (
                    self._new_state_id(instance_id, bootstrap_id)
                    if launch.projection is not None
                    else None
                )
                lease = SessionLease(
                    session=session,
                    owner=owner,
                    tool_name=tool_name,
                    bootstrap_id=bootstrap_id,
                    state_id=state_id,
                    deadline=time.monotonic()
                    + min(
                        UNCLAIMED_SESSION_TIMEOUT,
                        self._session_idle_timeout,
                    ),
                )
                self._sessions[instance_id] = lease
                self._bootstraps[bootstrap_id] = lease
                if state_id is not None:
                    self._state_handles[state_id] = lease
                self._owned_leases[owner] = lease
            self._task_group.start_soon(self._expire, instance_id, lease)
            return launch_result(
                lease,
                launch,
                tool_title=tool_title,
                loading_message=loading_message,
                app_uri=self._app_uri,
                session_idle_timeout_ms=self._session_idle_timeout * 1000,
            )
        except BaseException as error:
            with anyio.CancelScope(shield=True):
                taken: SessionLease | None = None
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
    def use(
        self,
        instance_id: str,
        *,
        renew: bool = True,
    ) -> Generator[SessionLease, None, None]:
        """Borrow a live lease and optionally renew its idle deadline."""

        with self._lock:
            lease = self._sessions.get(instance_id)
            if lease is not None:
                if lease.active_calls == 0:
                    lease.idle = anyio.Event()
                lease.active_calls += 1
                if renew:
                    lease.deadline = time.monotonic() + self._session_idle_timeout
        if lease is None:
            raise ToolError(f"Unknown widget session: {instance_id}")
        try:
            yield lease
        finally:
            with self._lock:
                lease.active_calls -= 1
                if renew and self._sessions.get(instance_id) is lease:
                    lease.deadline = time.monotonic() + self._session_idle_timeout
                if lease.active_calls == 0:
                    lease.idle.set()

    def bootstrap(self, bootstrap_id: str, operation_id: str) -> CallToolResult:
        """Claim a bootstrap capability and replay its initial runtime payload.

        The first operation ID owns the capability. Repeating that ID returns the
        original result, while a different operation ID is rejected.
        """

        validate_bootstrap_id(bootstrap_id)
        validate_operation_id(operation_id)
        with self._lock:
            lease = self._bootstraps.get(bootstrap_id)
            if lease is None:
                raise ToolError("Widget bootstrap is unavailable")
            claimed_by = lease.bootstrap_operation_id
            if claimed_by is not None and claimed_by != operation_id:
                raise ToolError("Widget bootstrap was claimed by another operation")

            replay = lease.bootstrap_replay
            if replay is None:
                payload = lease.bootstrap_payload
                if payload is None:
                    raise ToolError("Widget bootstrap is unavailable")
                replay = CallToolResult(
                    content=[],
                    _meta={"anywidget": copy.deepcopy(payload)},
                )
                lease.bootstrap_operation_id = operation_id
                lease.bootstrap_replay = replay.model_copy(deep=True)
                lease.bootstrap_payload = None
            lease.established = True
            lease.deadline = time.monotonic() + self._session_idle_timeout
            return replay.model_copy(deep=True)

    def state(self, state_id: str) -> tuple[str, ProjectionUpdate]:
        """Read current state without draining pending browser delivery.

        Reads before bootstrap preserve the shorter unclaimed-session deadline.
        """

        validate_state_id(state_id)
        with self._lock:
            lease = self._state_handles.get(state_id)
            renew = lease.established if lease is not None else False
        if lease is None:
            raise ToolError("Widget state is unavailable")

        try:
            with self.use(lease.session.instance_id, renew=renew) as active_lease:
                if active_lease is not lease:
                    raise ToolError("Widget state is unavailable")
                with lease.protocol_lock:
                    projection = lease.session.current_projection()
        except ToolError as error:
            if str(error).startswith("Unknown widget session:"):
                raise ToolError("Widget state is unavailable") from error
            raise
        except Exception as error:
            raise ToolError(f"Widget state projection failed: {error}") from error
        if projection is None:
            raise ToolError("Widget state is unavailable")
        return lease.tool_name, projection

    async def dispose(
        self,
        session_id: str,
        reason: str,
        operation_id: str | None = None,
    ) -> bool:
        lease = self._take(
            session_id,
            bootstrap_operation_id=operation_id,
            enforce_bootstrap_claim=True,
        )
        if lease is None:
            return False
        with anyio.CancelScope(shield=True):
            await self._finish_close(lease, reason)
        return True

    async def aclose(self) -> None:
        """Stop launches and close active, closing, and acquiring factories."""

        with anyio.CancelScope(shield=True):
            async with self._close_lock:
                with self._lock:
                    self._accepting = False
                    leases = list(self._sessions.values())
                    self._sessions.clear()
                    bootstrap_assets = [
                        (lease, self._forget_bootstrap_locked(lease))
                        for lease in leases
                    ]
                    self._bootstraps.clear()
                    self._state_handles.clear()
                    owners = set(self._owners)
                    active_owners = {lease.owner for lease in leases}
                    closing_owners = set(self._owned_leases) - active_owners
                    pending_owners = owners - set(self._owned_leases)
                    for lease in leases:
                        self._cancel_expiry(lease)

                for lease, asset_ids in bootstrap_assets:
                    lease.session.release_assets(asset_ids)

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

    def _new_state_id(self, instance_id: str, bootstrap_id: str) -> str:
        state_id = secrets.token_hex(16)
        while state_id in self._state_handles or state_id in {
            instance_id,
            bootstrap_id,
        }:
            state_id = secrets.token_hex(16)
        return state_id

    def _take(
        self,
        session_id: str,
        expected: SessionLease | None = None,
        *,
        bootstrap_operation_id: str | None = None,
        enforce_bootstrap_claim: bool = False,
    ) -> SessionLease | None:
        bootstrap_asset_ids: tuple[str, ...] = ()
        with self._lock:
            lease = self._sessions.get(session_id)
            bootstrap_handle = lease is None
            if lease is None:
                lease = self._bootstraps.get(session_id)
            if lease is None or (expected is not None and lease is not expected):
                return None
            if bootstrap_handle and enforce_bootstrap_claim:
                if bootstrap_operation_id is None:
                    raise ToolError(
                        "operation_id is required for a bootstrap capability"
                    )
                validate_operation_id(bootstrap_operation_id)
                claimed_by = lease.bootstrap_operation_id
                if claimed_by is not None and claimed_by != bootstrap_operation_id:
                    raise ToolError("Widget bootstrap was claimed by another operation")
            instance_id = lease.session.instance_id
            if self._sessions.get(instance_id) is not lease:
                return None
            del self._sessions[instance_id]
            bootstrap_asset_ids = self._forget_bootstrap_locked(lease)
            self._forget_state_handle_locked(lease)
            self._cancel_expiry(lease)
        lease.session.release_assets(bootstrap_asset_ids)
        return lease

    def complete_bootstrap(self, lease: SessionLease) -> None:
        """Retire bootstrap replay and release its asset pins after app activity."""

        with self._lock:
            if lease.bootstrap_operation_id is None:
                return
            asset_ids = self._forget_bootstrap_locked(lease)
        lease.session.release_assets(asset_ids)

    def _forget_state_handle_locked(self, lease: SessionLease) -> None:
        state_id = lease.state_id
        if state_id is not None and self._state_handles.get(state_id) is lease:
            del self._state_handles[state_id]

    def _forget_bootstrap_locked(self, lease: SessionLease) -> tuple[str, ...]:
        if self._bootstraps.get(lease.bootstrap_id) is lease:
            del self._bootstraps[lease.bootstrap_id]
        asset_ids = lease.bootstrap_asset_ids
        lease.bootstrap_asset_ids = ()
        lease.bootstrap_payload = None
        lease.bootstrap_operation_id = None
        lease.bootstrap_replay = None
        return asset_ids

    async def _finish_close(self, lease: SessionLease, reason: str) -> None:
        with anyio.CancelScope(shield=True):
            await lease.idle.wait()
            await self._finish_owner_close(lease.owner, reason)

    async def _finish_owner_close(
        self,
        owner: FactoryOwner,
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

    def _forget_owner(self, owner: FactoryOwner) -> None:
        with self._lock:
            self._owners.discard(owner)
            self._owned_leases.pop(owner, None)

    async def _expire(self, instance_id: str, lease: SessionLease) -> None:
        expired = False
        bootstrap_asset_ids: tuple[str, ...] = ()
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
                    bootstrap_asset_ids = self._forget_bootstrap_locked(lease)
                    self._forget_state_handle_locked(lease)
                    lease.expiry_scope = None
                    expired = True
                break
        if expired:
            lease.session.release_assets(bootstrap_asset_ids)
            try:
                await self._finish_close(lease, "idle expiry")
            except BaseException as error:
                logger.error("Widget cleanup failed during idle expiry: %s", error)

    @staticmethod
    def _cancel_expiry(lease: SessionLease) -> None:
        scope = lease.expiry_scope
        lease.expiry_scope = None
        if scope is not None:
            scope.cancel()


def launch_result(
    lease: SessionLease,
    launch: SessionSnapshot,
    *,
    tool_title: str,
    loading_message: str,
    app_uri: str,
    session_idle_timeout_ms: float,
) -> CallToolResult:
    """Build a launch result and retain its browser bootstrap payload.

    Source assets needed by the initial graph stay pinned until the bootstrap
    capability is retired or the lease closes.
    """

    projection = launch.projection
    structured: dict[str, Any] = {"tool": lease.tool_name}
    if projection is None:
        text = f"Opened {tool_title}."
    else:
        assert lease.state_id is not None
        structured["state"] = projection.state
        structured["state_id"] = lease.state_id
        text = (
            f"Opened {tool_title} with state {canonical_json(projection.state)}. "
            "To read later user changes, call anywidget_state with "
            f'{{"state_id":"{lease.state_id}"}}.'
        )

    runtime: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "instanceId": lease.session.instance_id,
        "rootModelId": lease.session.root_model_id,
        "sessionIdleTimeoutMs": session_idle_timeout_ms,
        "loadingMessage": loading_message,
        "assetManifest": launch.asset_manifest,
        "models": launch.models,
        "messages": launch.messages,
    }
    if projection is not None:
        runtime["context"] = context_payload(lease.tool_name, projection)
    lease.bootstrap_payload = runtime
    lease.bootstrap_asset_ids = lease.session.pin_assets(launch.asset_manifest)
    return CallToolResult(
        content=[
            TextContent(type="text", text=text),
            TextContent(
                type="text",
                text=f"{BOOTSTRAP_MARKER_PREFIX}{lease.bootstrap_id}",
            ),
        ],
        structuredContent=structured,
        _meta={"ui": {"resourceUri": app_uri}},
    )


def session_result(
    lease: SessionLease,
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
        payload["context"] = context_payload(
            lease.tool_name,
            snapshot.projection,
        )
    return CallToolResult(
        content=[],
        _meta={"anywidget": payload},
    )


def comm_fingerprint(
    model_id: str,
    data: dict[str, Any],
    buffers: Sequence[str],
) -> str:
    return canonical_json(
        {
            "modelId": model_id,
            "data": data,
            "buffers": list(buffers),
        }
    )


def validate_operation_id(operation_id: str) -> None:
    if not operation_id or len(operation_id) > 128:
        raise ToolError("operation_id must contain between 1 and 128 characters")


def validate_bootstrap_id(bootstrap_id: str) -> None:
    if BOOTSTRAP_ID_PATTERN.fullmatch(bootstrap_id) is None:
        raise ToolError("Widget bootstrap is unavailable")


def validate_state_id(state_id: str) -> None:
    if BOOTSTRAP_ID_PATTERN.fullmatch(state_id) is None:
        raise ToolError("Widget state is unavailable")


def remember_comm_replay(
    lease: SessionLease,
    operation_id: str,
    replay: CommReplay,
) -> None:
    """Cache a comm replay and release asset pins from the oldest eviction."""

    replay.asset_ids = lease.session.pin_assets(replay.asset_ids)
    lease.comm_replays[operation_id] = replay
    if len(lease.comm_replays) > COMM_REPLAY_LIMIT:
        _, evicted = lease.comm_replays.popitem(last=False)
        lease.session.release_assets(evicted.asset_ids)


def context_payload(
    tool_name: str,
    projection: ProjectionUpdate,
) -> dict[str, Any]:
    return {
        "version": projection.version,
        "tool": tool_name,
        "state": projection.state,
    }
