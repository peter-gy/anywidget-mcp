"""Acquire widget factories, normalize their outputs, and coordinate cleanup."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence, Set
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
)
from dataclasses import dataclass
from typing import Any, NoReturn, cast

import anyio
from anywidget import AnyWidget
from mcp.server.fastmcp.exceptions import ToolError

from ._bridge import WidgetSession
from ._group import _WidgetGroup
from ._widget_protocol import close_unclaimed_widget_graphs


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
        raise ToolError(
            f"Widget factory {origin} {type(value).__name__}, expected AnyWidget "
            "or a non-empty sequence of AnyWidget instances"
        )

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
        self.session: WidgetSession | None = None
        self.error: BaseException | None = None
        self.cleanup_error: BaseException | None = None
        self._acquisition_scope: anyio.CancelScope | None = None
        self._close_unowned_output = True
        self._close_reason = "session cleanup"

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
                                value = await stack.enter_async_context(value)
                                origin = "context manager yielded"
                            elif isinstance(value, AbstractContextManager):
                                value = stack.enter_context(value)
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
            self.error = error
        finally:
            self.ready.set()
            self.closed.set()

    async def wait_ready(self) -> WidgetOutput:
        await self.ready.wait()
        if self.output is not None:
            return self.output
        if self.error is not None:
            raise self.error
        raise RuntimeError("Widget factory closed before yielding a widget")

    def assign_session(self, session: WidgetSession) -> None:
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
