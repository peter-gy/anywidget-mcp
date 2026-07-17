from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, cast

from anywidget import AnyWidget

NOTIFICATION_WAIT_SECONDS = 3.0


class NotificationGate:
    """Keep trait notification chains atomic across widget snapshots."""

    def __init__(
        self,
        lock: threading.RLock,
        *,
        is_closed: Callable[[], bool],
    ) -> None:
        self.condition = threading.Condition(lock)
        self._is_closed = is_closed
        self._depths: dict[int, int] = {}
        self._sources: dict[tuple[int, int], int] = {}
        self._active_changes: dict[tuple[int, int], list[object]] = {}
        self._originals: dict[int, Callable[[Any], Any]] = {}

    @property
    def has_pending_restores(self) -> bool:
        return bool(self._originals)

    @property
    def pending_identities(self) -> set[int]:
        return set(self._originals)

    def has_gate(self, identity: int) -> bool:
        return identity in self._originals

    def active_changes(self, source_key: tuple[int, int]) -> tuple[object, ...]:
        return tuple(self._active_changes.get(source_key, ()))

    def source_depth(self, source_key: tuple[int, int]) -> int:
        return self._sources.get(source_key, 0)

    def notify_all(self) -> None:
        self.condition.notify_all()

    def wait(self, action: str) -> None:
        thread_id = threading.get_ident()
        if self._depths.get(thread_id, 0):
            raise RuntimeError(
                f"Cannot {action} a widget session from inside an active "
                "widget notification"
            )
        deadline = time.monotonic() + NOTIFICATION_WAIT_SECONDS
        while self._depths:
            if action == "snapshot" and self._is_closed():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out after {NOTIFICATION_WAIT_SECONDS:g} seconds waiting "
                    f"for widget notifications before {action}"
                )
            self.condition.wait(remaining)

    def install(self, widgets: Iterable[object]) -> None:
        for widget in widgets:
            identity = id(widget)
            if identity in self._originals:
                continue
            original = getattr(widget, "notify_change", None)
            if not callable(original):
                continue
            original = cast(Callable[[Any], Any], original)

            def notify_change(
                change: Any,
                original: Callable[[Any], Any] = original,
                identity: int = identity,
            ) -> None:
                thread_id = threading.get_ident()
                source_key = (thread_id, identity)
                with self.condition:
                    if self._is_closed():
                        return
                    self._depths[thread_id] = self._depths.get(thread_id, 0) + 1
                    self._sources[source_key] = self._sources.get(source_key, 0) + 1
                    self._active_changes.setdefault(source_key, []).append(change)
                try:
                    original(change)
                finally:
                    with self.condition:
                        active_changes = self._active_changes[source_key]
                        active_changes.pop()
                        if not active_changes:
                            self._active_changes.pop(source_key)
                        source_depth = self._sources[source_key] - 1
                        if source_depth:
                            self._sources[source_key] = source_depth
                        else:
                            self._sources.pop(source_key)
                        depth = self._depths[thread_id] - 1
                        if depth:
                            self._depths[thread_id] = depth
                        else:
                            self._depths.pop(thread_id)
                        if not self._depths:
                            self.condition.notify_all()

            self._originals[identity] = original
            try:
                setattr(widget, "notify_change", notify_change)
            except (AttributeError, TypeError):
                current = getattr(widget, "notify_change", None)
                same_callable = current is original or (
                    getattr(current, "__self__", None)
                    is getattr(original, "__self__", None)
                    and getattr(current, "__func__", None)
                    is getattr(original, "__func__", None)
                )
                if same_callable:
                    self._originals.pop(identity, None)
                if isinstance(widget, AnyWidget):
                    raise
                if same_callable:
                    continue
                raise

    def restore(self, widget: object) -> None:
        identity = id(widget)
        original = self._originals.get(identity)
        if original is not None:
            setattr(widget, "notify_change", original)
            self._originals.pop(identity, None)
