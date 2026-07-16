from __future__ import annotations

from typing import Any

import pytest
from anywidget._descriptor import MimeBundleDescriptor

from anywidget_mcp._bridge import (
    SessionSnapshot,
    WidgetInUseError,
    WidgetSession,
)
from anywidget_mcp._state import StateContext

from .bridge_test_widgets import (
    ChildWidget,
    NestedParentWidget,
    ParentWidget,
    ProtocolContainer,
)


def test_serialization_rollback_retains_detached_protocol_controller() -> None:
    class BrokenProtocolChild:
        @property
        def _repr_mimebundle_(self) -> object:
            raise RuntimeError("nested state failed")

    first = ProtocolContainer(payload={})
    root = NestedParentWidget(payload=first)
    session = WidgetSession("instance", root)
    replacement = ProtocolContainer(payload={})

    try:
        root.payload = replacement
        removed_model_ids = session.snapshot().removed_model_ids

        with pytest.raises(RuntimeError, match="nested state failed"):
            root.payload = BrokenProtocolChild()

        assert id(first) in session._protocol_controllers
        session.acknowledge_model_removals(removed_model_ids)
        assert id(first) not in session._protocol_controllers
    finally:
        session.close()


def test_detached_cleanup_retry_skips_completed_widget_close() -> None:
    class RetryGateChild(ChildWidget):
        def __init__(self) -> None:
            self.fail_gate_restore = False
            self.close_calls = 0
            super().__init__()

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "notify_change" and getattr(
                self,
                "fail_gate_restore",
                False,
            ):
                super().__setattr__("fail_gate_restore", False)
                raise RuntimeError("notification gate restore failed")
            super().__setattr__(name, value)

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls > 1:
                return
            super().close()

    child = RetryGateChild()
    parent = ParentWidget(child=child)
    session = WidgetSession("instance", parent)

    try:
        child.fail_gate_restore = True
        parent.child = None
        removed_model_ids = session.snapshot().removed_model_ids

        with pytest.raises(ExceptionGroup, match="detached widget models"):
            session.acknowledge_model_removals(removed_model_ids)
        session.acknowledge_model_removals(removed_model_ids)

        assert child.close_calls == 1
        assert child.comm is None
    finally:
        session.close()


def test_partial_notification_gate_install_is_restored() -> None:
    class PartialGateWidget(ChildWidget):
        def __init__(self) -> None:
            self.raise_after_gate_assignment = False
            super().__init__()
            self.raise_after_gate_assignment = True

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "notify_change" and getattr(
                self,
                "raise_after_gate_assignment",
                False,
            ):
                super().__setattr__(name, value)
                super().__setattr__("raise_after_gate_assignment", False)
                raise RuntimeError("notification gate install failed")
            super().__setattr__(name, value)

    widget = PartialGateWidget()
    original_notify_change = widget.notify_change

    with pytest.raises(RuntimeError, match="notification gate install failed"):
        WidgetSession("instance", widget)

    assert widget.notify_change == original_notify_change
    assert widget.comm is None


def test_enrollment_cleanup_preserves_completed_widget_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryGraphCleanupChild(ChildWidget):
        def __init__(self) -> None:
            self.unobserve_attempts = 0
            self.close_calls = 0
            super().__init__()

        def unobserve(self, *args: Any, **kwargs: Any) -> None:
            handler = args[0]
            if getattr(handler, "__name__", None) == "_sync_widget_graph":
                self.unobserve_attempts += 1
                if self.unobserve_attempts == 1:
                    raise RuntimeError("graph observer cleanup failed")
            super().unobserve(*args, **kwargs)

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls > 1:
                return
            super().close()

    def fail_state_enrollment(
        _context: StateContext,
        _widgets: list[object],
    ) -> None:
        raise RuntimeError("state enrollment failed")

    parent = ParentWidget(child=None)
    session = WidgetSession("instance", parent)
    child = RetryGraphCleanupChild()
    monkeypatch.setattr(StateContext, "add_widgets", fail_state_enrollment)

    with pytest.raises(ExceptionGroup, match="nested widget model"):
        parent.child = child

    assert child.close_calls == 1
    assert child.unobserve_attempts == 2
    assert child.comm is None
    assert session.snapshot() == SessionSnapshot([], {}, {}, [], None, None)
    session.close()


def test_state_cleanup_failure_retains_nonweak_protocol_claim() -> None:
    class SlottedProtocolChild:
        __slots__ = ("observers", "unobserve_attempts", "value")

        _repr_mimebundle_ = MimeBundleDescriptor(
            _esm="export default { render() {} }",
            autodetect_observer=False,
            follow_changes=False,
        )

        def __init__(self) -> None:
            self.value = 1
            self.observers: list[tuple[object, tuple[str, ...]]] = []
            self.unobserve_attempts = 0

        def _get_anywidget_state(
            self,
            include: set[str] | None,
        ) -> dict[str, object]:
            return {"value": self.value}

        def trait_names(self) -> list[str]:
            return ["value"]

        def observe(self, callback: object, *, names: tuple[str, ...]) -> None:
            self.observers.append((callback, names))

        def unobserve(self, callback: object, *, names: tuple[str, ...]) -> None:
            if isinstance(getattr(callback, "__self__", None), StateContext):
                self.unobserve_attempts += 1
                if self.unobserve_attempts == 1:
                    raise RuntimeError("state observer cleanup failed")
            self.observers.remove((callback, names))

    child = SlottedProtocolChild()
    first_root = NestedParentWidget(payload=child)
    with pytest.warns(UserWarning, match="not weakrefable"):
        first = WidgetSession("first", first_root, lambda _widget: {})

    with pytest.raises(ExceptionGroup, match="Failed to close widget session"):
        first.close()

    second_root = NestedParentWidget(payload=child)
    with pytest.warns(UserWarning, match="not weakrefable"):
        with pytest.raises(WidgetInUseError, match="fresh nested widgets"):
            WidgetSession("second", second_root, lambda _widget: {})

    first.close()
    third_root = NestedParentWidget(payload=child)
    with pytest.warns(UserWarning, match="not weakrefable"):
        second = WidgetSession("second", third_root, lambda _widget: {})
    second.close()

    assert child.unobserve_attempts == 3
