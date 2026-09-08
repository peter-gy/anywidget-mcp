from __future__ import annotations

import gc
from typing import Any
import weakref

import pytest
from anywidget._descriptor import MimeBundleDescriptor

from anywidget_mcp._bridge import WidgetInUseError, WidgetSession
from anywidget_mcp._comm import BridgeComm
from anywidget_mcp._state import StateContext

from .bridge_test_widgets import (
    ChildWidget,
    EqualWidget,
    NestedParentWidget,
    ParentWidget,
    UnhashableWidget,
)


def test_close_disposes_every_model_and_rejects_messages() -> None:
    child = ChildWidget()
    parent = ParentWidget(child=child)
    session = WidgetSession("instance", parent)
    parent_id = session.root_model_id

    session.close()
    session.close()

    assert parent.comm is None
    assert child.comm is None
    assert session.snapshot().messages == []
    with pytest.raises(RuntimeError, match="widget session is closed"):
        session.receive(parent_id, {"method": "request_state"})


def test_close_attempts_every_widget_after_one_cleanup_fails() -> None:
    closed: list[str] = []

    class BrokenChild(ChildWidget):
        def close(self) -> None:
            fail = getattr(self, "_fail_close", True)
            self._fail_close = False
            super().close()
            closed.append("child")
            if fail:
                raise RuntimeError("child close failed")

    class TrackedParent(ParentWidget):
        def close(self) -> None:
            super().close()
            closed.append("parent")

    child = BrokenChild()
    parent = TrackedParent(child=child)
    session = WidgetSession("instance", parent)

    with pytest.raises(ExceptionGroup, match="Failed to close widget session"):
        session.close()

    assert closed == ["child", "parent"]
    assert child.comm is None
    assert parent.comm is None
    session.close()


def test_close_closes_comm_when_widget_fails_before_base_cleanup() -> None:
    class BrokenWidget(ChildWidget):
        def close(self) -> None:
            if getattr(self, "_fail_close", True):
                self._fail_close = False
                raise RuntimeError("widget close failed")
            super().close()

    widget = BrokenWidget()
    session = WidgetSession("instance", widget)
    comm = widget.comm

    with pytest.raises(ExceptionGroup, match="Failed to close widget session"):
        session.close()

    assert isinstance(comm, BridgeComm)
    with pytest.raises(RuntimeError, match="widget session is closed"):
        comm.receive({"method": "request_state"})
    session.close()
    assert widget.comm is None


def test_close_retries_state_observer_cleanup() -> None:
    class BrokenStateObserver(ChildWidget):
        attempts = 0

        def unobserve(self, *args: Any, **kwargs: Any) -> None:
            handler = args[0]
            if isinstance(getattr(handler, "__self__", None), StateContext):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("state observer cleanup failed")
            super().unobserve(*args, **kwargs)

    widget = BrokenStateObserver()
    session = WidgetSession(
        "instance", widget, lambda current: {"value": current.value}
    )

    with pytest.raises(ExceptionGroup, match="Failed to close widget session"):
        session.close()
    assert widget.attempts == 1

    session.close()
    assert widget.attempts == 2


def test_closed_widget_cannot_be_returned_by_another_tool_call() -> None:
    widget = ChildWidget()
    session = WidgetSession("first", widget)
    session.close()

    with pytest.raises(WidgetInUseError, match="fresh root"):
        WidgetSession("second", widget)


def test_closed_child_cannot_be_reused_in_a_fresh_root() -> None:
    child = ChildWidget()
    first_root = ParentWidget(child=child)
    second_root = ParentWidget(child=child)
    first = WidgetSession("first", first_root)
    first.close()

    with pytest.raises(WidgetInUseError, match="fresh nested widgets"):
        WidgetSession("second", second_root)

    assert second_root.comm is None


def test_nonweakrefable_slotted_protocol_releases_object_after_close() -> None:
    class Token:
        pass

    class SlottedProtocolChild:
        __slots__ = ("token", "value")

        _repr_mimebundle_ = MimeBundleDescriptor(
            _esm="export default { render() {} }",
            autodetect_observer=False,
            follow_changes=False,
        )

        def __init__(self, token: Token) -> None:
            self.token = token
            self.value = 1

        def _get_anywidget_state(
            self,
            include: set[str] | None,
        ) -> dict[str, object]:
            return {"value": self.value}

    token = Token()
    token_ref = weakref.ref(token)
    child = SlottedProtocolChild(token)
    first_root = NestedParentWidget(payload=child)
    with pytest.warns(UserWarning, match="not weakrefable"):
        session = WidgetSession("first", first_root)
    session.close()

    second_root = NestedParentWidget(payload=child)
    with pytest.warns(UserWarning, match="not weakrefable"):
        second = WidgetSession("second", second_root)
    second.close()

    del child, first_root, second_root, session, second, token
    gc.collect()
    assert token_ref() is None


def test_fresh_instance_tracking_accepts_unhashable_widgets() -> None:
    first = WidgetSession("first", UnhashableWidget())
    second = WidgetSession("second", UnhashableWidget())

    first.close()
    second.close()


def test_fresh_instance_tracking_uses_identity_for_equal_widgets() -> None:
    first = WidgetSession("first", EqualWidget())
    second = WidgetSession("second", EqualWidget())

    first.close()
    second.close()
