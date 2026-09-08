"""Acquire widget factories, normalize their outputs, and coordinate cleanup."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence, Set
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
    asynccontextmanager,
)
from contextvars import Context, copy_context
from dataclasses import dataclass
from functools import partial
from typing import Any, NoReturn, TypeVar, cast

from exceptiongroup import BaseExceptionGroup, ExceptionGroup
import anyio
from anywidget import AnyWidget
from mcp.server.mcpserver.exceptions import ToolError

from ._spec.ports import OwnedSession
from ._group import _WidgetGroup
from ._widget_protocol import close_unclaimed_widget_graphs

ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class WidgetOutput:
    """Normalized render root and model-state roots from a widget factory."""

    render_root: AnyWidget
    state_roots: tuple[AnyWidget, ...]
    sequence: bool


def normalize_widget_output(value: object, *, origin: str) -> WidgetOutput:
    """Normalize one widget or a non-empty sequence into session roots.

    A sequence renders through ``_WidgetGroup`` while retaining each widget as a
    state root. Validation failure closes widgets found in the returned graph.
    """

    if isinstance(value, AnyWidget):
        return WidgetOutput(
            render_root=value,
            state_roots=(value,),
            sequence=False,
        )

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        error = ToolError(
            f"Widget factory {origin} {type(value).__name__}, expected AnyWidget "
            "or a non-empty sequence of AnyWidget instances"
        )
        _raise_after_output_cleanup(error, (value,))

    snapshot: list[object] = []
    try:
        snapshot.extend(value)
    except BaseException as error:
        _raise_after_output_cleanup(error, snapshot)
    widgets = tuple(snapshot)
    if not widgets:
        raise ToolError(f"Widget factory {origin} an empty widget sequence")

    invalid = next(
        (
            (index, widget)
            for index, widget in enumerate(widgets)
            if not isinstance(widget, AnyWidget)
        ),
        None,
    )
    if invalid is not None:
        index, item = invalid
        error = ToolError(
            f"Widget factory {origin} {type(item).__name__} at sequence index "
            f"{index}, expected AnyWidget"
        )
        _raise_after_output_cleanup(error, widgets)

    typed_widgets = cast(tuple[AnyWidget, ...], widgets)
    try:
        render_root = _WidgetGroup(typed_widgets)
    except Exception as error:
        _raise_after_output_cleanup(error, typed_widgets)
    return WidgetOutput(
        render_root=render_root,
        state_roots=typed_widgets,
        sequence=True,
    )


def _raise_after_output_cleanup(
    error: BaseException,
    values: Sequence[object],
) -> NoReturn:
    widgets = _returned_widgets(values)
    try:
        close_unclaimed_widget_graphs(widgets)
    except Exception as cleanup_error:
        errors = [error, cleanup_error]
        if all(isinstance(item, Exception) for item in errors):
            raise ExceptionGroup(
                "Widget factory result validation and cleanup failed",
                cast(list[Exception], errors),
            ) from error
        raise BaseExceptionGroup(
            "Widget factory result validation and cleanup failed",
            errors,
        ) from error
    raise error


def _returned_widgets(values: Sequence[object]) -> tuple[AnyWidget, ...]:
    """Collect widgets from cyclic or partially malformed containers for cleanup."""

    widgets: list[AnyWidget] = []
    pending = list(reversed(values))
    seen_containers: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, AnyWidget):
            widgets.append(value)
            continue
        if isinstance(value, (str, bytes, bytearray, memoryview)):
            continue
        if not isinstance(value, (Mapping, Sequence, Set)):
            continue
        identity = id(value)
        if identity in seen_containers:
            continue
        seen_containers.add(identity)

        nested: list[object] = []
        try:
            items = value.values() if isinstance(value, Mapping) else value
            iterator = iter(items)
            while True:
                nested.append(next(iterator))
        except StopIteration:
            pass
        except Exception:
            # A malformed result must not prevent already discovered widgets from
            # closing. Treat the unread portion as opaque and continue cleanup.
            pass
        pending.extend(reversed(nested))
    return tuple(widgets)


class FactoryOwner:
    """Own one factory acquisition until its widget session closes.

    Synchronous and asynchronous context managers remain entered for the
    session lifetime. Cleanup is shielded from request cancellation.
    """

    def __init__(self) -> None:
        self.ready = anyio.Event()
        self.close_requested = anyio.Event()
        self.closed = anyio.Event()
        self.output: WidgetOutput | None = None
        self.session: OwnedSession | None = None
        self.error: BaseException | None = None
        self.cleanup_error: BaseException | None = None
        self._hold_cancellation: BaseException | None = None
        self._acquisition_scope: anyio.CancelScope | None = None
        self._close_unowned_output = True
        self._close_reason = "session cleanup"
        self._sync_context: Context | None = None

    async def run(
        self,
        candidate: Any,
        arguments: dict[str, Any],
    ) -> None:
        """Acquire factory output, publish it when ready, and hold its resources."""

        try:
            with anyio.CancelScope(shield=True):
                # Manager exits stay outside the cancellable acquisition scope so
                # post-yield cleanup can await during request cancellation.
                async with AsyncExitStack() as stack:
                    if self.close_requested.is_set():
                        arguments.clear()
                        return
                    output: WidgetOutput | None = None
                    with anyio.CancelScope() as acquisition_scope:
                        self._acquisition_scope = acquisition_scope
                        try:
                            if self.close_requested.is_set():
                                arguments.clear()
                                return
                            try:
                                if inspect.iscoroutinefunction(
                                    candidate
                                ) or inspect.iscoroutinefunction(
                                    getattr(candidate, "__call__", None)
                                ):
                                    created = candidate(**arguments)
                                else:
                                    created = await self._run_sync(
                                        partial(candidate, **arguments)
                                    )
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
                                value = await stack.enter_async_context(value)
                                origin = "context manager yielded"
                            elif isinstance(value, AbstractContextManager):
                                value = await stack.enter_async_context(
                                    self._sync_manager(value)
                                )
                                origin = "context manager yielded"
                            else:
                                origin = "returned"
                            output = normalize_widget_output(value, origin=origin)
                        finally:
                            self._acquisition_scope = None
                    if acquisition_scope.cancel_called:
                        if output is not None:
                            self.output = output
                            self._cleanup_once()
                        return
                    assert output is not None
                    await self._hold(output)
        except BaseException as error:
            # Native task cancellation bypasses AnyIO shielding. The original
            # hold cancellation reaches here after the managers have unwound.
            if error is not self._hold_cancellation:
                self.error = error
        finally:
            self._hold_cancellation = None
            self.ready.set()
            self.closed.set()

    async def _run_sync(
        self,
        function: Callable[[], ResultT],
        *,
        limiter: anyio.CapacityLimiter | None = None,
    ) -> ResultT:
        if self._sync_context is None:
            self._sync_context = copy_context()
        context = self._sync_context
        result: list[ResultT] = []
        error: list[BaseException] = []

        async def run() -> None:
            try:
                result.append(
                    await anyio.to_thread.run_sync(
                        context.run, function, limiter=limiter
                    )
                )
            except BaseException as caught:
                error.append(caught)

        try:
            # The child joins its worker even when native task cancellation bypasses
            # the owner's shield. Retain the output so acquisition can roll it back.
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(run)
        except anyio.get_cancelled_exc_class():
            if self._acquisition_scope is None or not result:
                raise
            self._acquisition_scope.cancel()
        if error:
            raise error[0]
        return result[0]

    @asynccontextmanager
    async def _sync_manager(
        self, manager: AbstractContextManager[Any]
    ) -> AsyncGenerator[Any]:
        # Entry can fill the worker pool while waiting for a resource held by
        # another manager. Exit must remain able to release that resource.
        exit_limiter = anyio.CapacityLimiter(1)
        value = await self._run_sync(manager.__enter__)
        try:
            yield value
        except BaseException as error:
            if not await self._run_sync(
                partial(
                    manager.__exit__,
                    type(error),
                    error,
                    error.__traceback__,
                ),
                limiter=exit_limiter,
            ):
                raise
        else:
            await self._run_sync(
                partial(manager.__exit__, None, None, None), limiter=exit_limiter
            )

    async def wait_ready(self) -> WidgetOutput:
        await self.ready.wait()
        if self.output is not None:
            return self.output
        if self.error is not None:
            raise self.error
        raise RuntimeError("Widget factory closed before yielding a widget")

    def assign_session(self, session: OwnedSession) -> None:
        self.session = session

    def leave_unowned_output_open(self) -> None:
        """Keep a widget graph owned by another live session open during cleanup."""
        self._close_unowned_output = False

    def request_close(self, reason: str) -> None:
        if not self.close_requested.is_set():
            self._close_reason = reason
            self.close_requested.set()
        if not self.ready.is_set() and self._acquisition_scope is not None:
            self._acquisition_scope.cancel()

    async def wait_closed(self) -> BaseException | None:
        await self.closed.wait()
        return self.error

    async def _hold(self, output: WidgetOutput) -> None:
        self.output = output
        self.ready.set()
        try:
            await self.close_requested.wait()
        except anyio.get_cancelled_exc_class() as error:
            self._hold_cancellation = error
            raise
        finally:
            self._cleanup_once()
            self.retry_cleanup()

    def retry_cleanup(self) -> BaseException | None:
        if self.cleanup_error is not None:
            self._cleanup_once()
        return self.cleanup_error

    def _cleanup_once(self) -> None:
        try:
            if self.session is not None:
                self.session.close()
                self.session = None
                self.output = None
            elif self._close_unowned_output and self.output is not None:
                close_unclaimed_widget_graphs((self.output.render_root,))
                self.output = None
        except BaseException as error:
            self.cleanup_error = error
        else:
            self.cleanup_error = None
