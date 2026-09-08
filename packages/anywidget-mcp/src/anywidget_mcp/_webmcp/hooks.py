"""Route native widget construction to the owning WebMCP session."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from anywidget import AnyWidget
from ipywidgets import Widget

if TYPE_CHECKING:
    from .instrument import _Instrumentation

_lock = threading.RLock()
_hooks: _Hooks | None = None
_creating: ContextVar[Invocation | None] = ContextVar(
    "webmcp_creation_owner", default=None
)


@dataclass
class Invocation:
    owner: _Instrumentation
    widgets: list[AnyWidget] = field(default_factory=list)
    active: bool = True


def current_creation() -> Invocation | None:
    invocation = _creating.get()
    return invocation if invocation is not None and invocation.active else None


class _Hooks:
    def __init__(self) -> None:
        self.owners: dict[_Instrumentation, int] = {}
        self.original = Widget._call_widget_constructed
        self.original_add_traits = Widget.add_traits

        def constructed(widget: Widget) -> None:
            owner = self.owner()
            with owner.host.scope() if owner is not None else nullcontext():
                if owner is not None and isinstance(widget, AnyWidget):
                    owner.prepare(widget)
                self.original(widget)

        def add_traits(widget: Widget, **traits: Any) -> None:
            owner = self.owner()
            with owner.host.scope() if owner is not None else nullcontext():
                self.original_add_traits(widget, **traits)
                if (
                    owner is not None
                    and isinstance(widget, AnyWidget)
                    and owner.tracks(widget)
                ):
                    owner.refresh(widget)

        self.constructed = constructed
        self.add_traits = add_traits
        # Intercept the dispatcher so hosts can replace their singleton callback.
        setattr(Widget, "_call_widget_constructed", staticmethod(constructed))
        setattr(Widget, "add_traits", add_traits)

    def owner(self) -> _Instrumentation | None:
        invocation = current_creation()
        if invocation is not None:
            return invocation.owner
        with _lock:
            return next(
                (
                    owner
                    for owner in self.owners
                    if owner.enabled and owner.host.is_current()
                ),
                None,
            )

    def close(self) -> None:
        if Widget._call_widget_constructed is self.constructed:
            setattr(Widget, "_call_widget_constructed", staticmethod(self.original))
        if Widget.add_traits is self.add_traits:
            setattr(Widget, "add_traits", self.original_add_traits)


def subscribe(owner: _Instrumentation) -> None:
    global _hooks
    with _lock:
        if _hooks is None:
            _hooks = _Hooks()
        _hooks.owners[owner] = _hooks.owners.get(owner, 0) + 1


def unsubscribe(owner: _Instrumentation) -> None:
    global _hooks
    with _lock:
        if _hooks is None:
            return
        count = _hooks.owners.get(owner, 0)
        if count > 1:
            _hooks.owners[owner] = count - 1
        else:
            _hooks.owners.pop(owner, None)
        if not _hooks.owners:
            _hooks.close()
            _hooks = None


@contextmanager
def creation(owner: _Instrumentation) -> Generator[Invocation, None, None]:
    # Keep capture alive until an invocation settles, including after cancellation.
    subscribe(owner)
    invocation = Invocation(owner)
    token = _creating.set(invocation)
    try:
        yield invocation
    finally:
        invocation.active = False
        invocation.widgets.clear()
        _creating.reset(token)
        unsubscribe(owner)
