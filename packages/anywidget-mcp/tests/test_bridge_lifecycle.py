from __future__ import annotations

import gc
import threading
from typing import Any
import weakref

import anywidget
import pytest
from anywidget._descriptor import MimeBundleDescriptor

import anywidget_mcp._bridge as bridge
from anywidget_mcp._bridge import (
    SessionSnapshot,
    WidgetInUseError,
    WidgetSession,
    WidgetSessionInitializationError,
)
from anywidget_mcp._state import StateContext, _RefreshStatus

from .bridge_test_widgets import (
    ChildWidget,
    EqualWidget,
    NestedParentWidget,
    ParentWidget,
    PausedNotifyWidget,
    ProjectionWidget,
    ProtocolContainer,
    UnhashableWidget,
)


def test_deferred_projection_retries_when_notification_finishes_before_drain() -> None:
    widget = PausedNotifyWidget()
    projection_started = threading.Event()
    mutation_done = threading.Event()
    calls = 0

    def project(current: anywidget.AnyWidget) -> dict[str, int]:
        nonlocal calls
        assert isinstance(current, PausedNotifyWidget)
        calls += 1
        if calls == 2:
            projection_started.set()
            if not current.value_stored.wait(1):
                raise RuntimeError("value storage test did not start")
        return {"trigger": current.trigger, "value": current.value}

    session = WidgetSession("instance", widget, project)
    context = session._state_context
    assert context is not None
    original_refresh = context.refresh

    def finish_notification_after_defer() -> _RefreshStatus:
        status = original_refresh()
        if status is _RefreshStatus.DEFERRED:
            widget.resume_value_notification.set()
            if not mutation_done.wait(1):
                raise RuntimeError("value notification test did not finish")
        return status

    setattr(context, "refresh", finish_notification_after_defer)

    def change_value() -> None:
        try:
            widget.value = 8
        finally:
            mutation_done.set()

    try:
        assert session.take_projection() is not None
        widget.trigger = 1
        widget.pause_value_notification = True
        snapshots: list[SessionSnapshot] = []
        capture = threading.Thread(target=lambda: snapshots.append(session.snapshot()))
        capture.start()
        assert projection_started.wait(1)
        mutation = threading.Thread(target=change_value)
        mutation.start()
        assert mutation_done.wait(1)
        capture.join()
        mutation.join()

        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.projection is not None
        assert snapshot.projection.state == {"trigger": 1, "value": 8}
        assert [message["data"]["state"] for message in snapshot.messages] == [
            {"trigger": 1},
            {"value": 8},
        ]
    finally:
        widget.resume_value_notification.set()
        session.close()


def test_projection_mutation_returns_one_context_error() -> None:
    widget = ProjectionWidget()

    def project(current: anywidget.AnyWidget) -> dict[str, int]:
        assert isinstance(current, ProjectionWidget)
        if current.a == 1 and current.b == 0:
            current.b = 2
        return {"a": current.a, "b": current.b}

    session = WidgetSession("instance", widget, project)
    try:
        assert session.take_projection() is not None
        widget.a = 1

        snapshot = session.snapshot()

        assert snapshot.projection is None
        assert snapshot.projection_error == (
            "State projection callables must not mutate synchronized widget traits"
        )
        assert [message["data"]["state"] for message in snapshot.messages] == [
            {"a": 1},
            {"b": 2},
        ]
        assert session.snapshot() == SessionSnapshot([], {}, {}, [], None, None)
    finally:
        session.close()


def test_blocking_projection_does_not_block_close_or_commit_late_state() -> None:
    widget = ProjectionWidget()
    projection_started = threading.Event()
    release_projection = threading.Event()
    snapshot_done = threading.Event()
    close_done = threading.Event()
    snapshots: list[SessionSnapshot] = []
    errors: list[BaseException] = []
    calls = 0

    def project(current: anywidget.AnyWidget) -> dict[str, int]:
        nonlocal calls
        assert isinstance(current, ProjectionWidget)
        calls += 1
        if calls == 2:
            projection_started.set()
            release_projection.wait()
        return {"a": current.a, "b": current.b}

    session = WidgetSession("instance", widget, project)
    context = session._state_context

    def take_snapshot() -> None:
        try:
            snapshots.append(session.snapshot())
        except BaseException as error:
            errors.append(error)
        finally:
            snapshot_done.set()

    def close_session() -> None:
        try:
            session.close()
        except BaseException as error:
            errors.append(error)
        finally:
            close_done.set()

    try:
        assert session.take_projection() is not None
        widget.a = 1
        capture = threading.Thread(target=take_snapshot)
        capture.start()
        assert projection_started.wait(1)

        closing = threading.Thread(target=close_session)
        closing.start()
        assert close_done.wait(0.1)
        closing.join()
        assert session.snapshot() == SessionSnapshot([], {}, {}, [], None, None)

        release_projection.set()
        assert snapshot_done.wait(1)
        capture.join()

        assert errors == []
        assert snapshots == [SessionSnapshot([], {}, {}, [], None, None)]
        assert context is not None
        assert context.take_cached() is None
    finally:
        release_projection.set()
        session.close()


def test_snapshot_times_out_for_stalled_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_NOTIFICATION_WAIT_SECONDS", 0.01)
    notification_started = threading.Event()
    resume_notification = threading.Event()
    mutation_done = threading.Event()
    widget = ChildWidget()

    def stall_notification(_change: object) -> None:
        notification_started.set()
        resume_notification.wait()

    widget.observe(stall_notification, names="value")
    session = WidgetSession("instance", widget)

    def mutate() -> None:
        try:
            widget.value = 8
        finally:
            mutation_done.set()

    mutation = threading.Thread(target=mutate)
    mutation.start()
    try:
        assert notification_started.wait(1)
        with pytest.raises(TimeoutError, match="before snapshot"):
            session.snapshot()
        resume_notification.set()
        assert mutation_done.wait(1)
        mutation.join()
        assert session.snapshot().messages
    finally:
        resume_notification.set()
        mutation.join()
        session.close()


def test_snapshot_rejects_active_notification_thread() -> None:
    errors: list[Exception] = []
    widget = ChildWidget()

    def take_reentrant_snapshot(_change: object) -> None:
        try:
            session.snapshot()
        except Exception as error:
            errors.append(error)

    widget.observe(take_reentrant_snapshot, names="value")
    session = WidgetSession("instance", widget)
    try:
        widget.value = 8

        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert "inside an active widget notification" in str(errors[0])
        assert session.snapshot().messages
    finally:
        session.close()


def test_close_cleans_up_after_stalled_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_NOTIFICATION_WAIT_SECONDS", 0.01)
    notification_started = threading.Event()
    resume_notification = threading.Event()
    mutation_done = threading.Event()
    widget_closed = threading.Event()

    class TrackedWidget(ChildWidget):
        def close(self) -> None:
            widget_closed.set()
            super().close()

    widget = TrackedWidget()
    original_notify_change = widget.notify_change

    def stall_notification(_change: object) -> None:
        notification_started.set()
        resume_notification.wait()

    widget.observe(stall_notification, names="value")
    session = WidgetSession("instance", widget)
    comm = widget.comm

    def mutate() -> None:
        try:
            widget.value = 8
        finally:
            mutation_done.set()

    mutation = threading.Thread(target=mutate)
    mutation.start()
    try:
        assert notification_started.wait(1)
        with pytest.raises(ExceptionGroup) as error_info:
            session.close()

        assert any(
            isinstance(error, TimeoutError) for error in error_info.value.exceptions
        )
        assert widget_closed.is_set()
        assert widget.notify_change == original_notify_change
        assert session.snapshot() == SessionSnapshot([], {}, {}, [], None, None)
        assert comm is not None
        with pytest.raises(RuntimeError, match="widget session is closed"):
            comm.receive({"method": "request_state"})
    finally:
        resume_notification.set()
        assert mutation_done.wait(1)
        mutation.join()
        session.close()


def test_reentrant_close_cleans_up_before_raising() -> None:
    errors: list[BaseException] = []
    widget_closed = False

    class TrackedWidget(ChildWidget):
        def close(self) -> None:
            nonlocal widget_closed
            widget_closed = True
            super().close()

    widget = TrackedWidget()

    def close_from_notification(_change: object) -> None:
        try:
            session.close()
        except BaseException as error:
            errors.append(error)

    widget.observe(close_from_notification, names="value")
    session = WidgetSession("instance", widget)
    comm = widget.comm
    widget.value = 8

    assert widget_closed
    assert len(errors) == 1
    assert isinstance(errors[0], ExceptionGroup)
    assert any(
        isinstance(error, RuntimeError)
        and "inside an active widget notification" in str(error)
        for error in errors[0].exceptions
    )
    assert session.snapshot() == SessionSnapshot([], {}, {}, [], None, None)
    assert comm is not None
    with pytest.raises(RuntimeError, match="widget session is closed"):
        comm.receive({"method": "request_state"})
    session.close()


def test_widget_trait_replacement_survives_detached_cleanup_failure() -> None:
    replacement_closed = False

    class BrokenDetachedChild(ChildWidget):
        def close(self) -> None:
            if getattr(self, "_fail_close", True):
                self._fail_close = False
                raise RuntimeError("detached child close failed")
            super().close()

    class TrackedReplacement(ChildWidget):
        def close(self) -> None:
            nonlocal replacement_closed
            replacement_closed = True
            super().close()

    first = BrokenDetachedChild(value=1)
    parent = ParentWidget(child=first)
    session = WidgetSession("instance", parent)
    first_comm = first.comm
    second = TrackedReplacement(value=2)

    try:
        parent.child = second

        assert set(session.models) == {parent.model_id, second.model_id}
        snapshot = session.receive(
            second.model_id,
            {"method": "request_state"},
        )
        assert snapshot.messages
        assert first_comm is not None
        with pytest.raises(
            ExceptionGroup,
            match="Failed to finalize detached widget models",
        ):
            session.acknowledge_model_removals(snapshot.removed_model_ids)
        with pytest.raises(RuntimeError, match="widget session is closed"):
            first_comm.receive({"method": "request_state"})
    finally:
        session.close()

    assert first.comm is None
    assert replacement_closed


def test_detached_protocol_controller_is_released_after_acknowledgment() -> None:
    first = ProtocolContainer(payload={})
    parent = NestedParentWidget(payload=first)
    session = WidgetSession("instance", parent)
    second = ProtocolContainer(payload={})

    try:
        assert id(first) in session._protocol_controllers
        parent.payload = second
        snapshot = session.snapshot()
        session.acknowledge_model_removals(snapshot.removed_model_ids)

        assert id(first) not in session._protocol_controllers
        assert id(second) in session._protocol_controllers
    finally:
        session.close()

    assert session._protocol_controllers == {}


def test_rapid_widget_trait_replacement_preserves_transient_model_messages() -> None:
    first = ChildWidget(value=1)
    parent = ParentWidget(child=first)
    session = WidgetSession("instance", parent)
    second = ChildWidget(value=2)
    third = ChildWidget(value=3)
    first_model_id = first.model_id
    second_model_id = second.model_id

    try:
        parent.child = second
        first_comm = first.comm
        second_comm = second.comm
        second.value = 20
        parent.child = third
        snapshot = session.snapshot()

        assert set(snapshot.models) == {second_model_id, third.model_id}
        assert snapshot.removed_model_ids == [first_model_id, second_model_id]
        assert any(
            message["modelId"] == second_model_id
            and message["data"].get("state", {}).get("value") == 20
            for message in snapshot.messages
        )
        assert first.comm is first_comm
        assert second.comm is second_comm

        session.acknowledge_model_removals(snapshot.removed_model_ids)
        assert first.comm is None
        assert second.comm is None
    finally:
        session.close()


def test_reused_dynamic_child_rolls_back_without_foreign_messages() -> None:
    foreign = ChildWidget(value=4)
    foreign_session = WidgetSession("foreign", foreign)
    original = ChildWidget(value=5)
    parent = ParentWidget(child=original)
    session = WidgetSession("instance", parent)
    foreign_model_id = foreign.model_id

    try:
        with pytest.raises(WidgetInUseError, match="fresh nested widgets"):
            parent.child = foreign

        assert parent.child is original
        assert foreign.comm is not None
        assert foreign_session.receive(
            foreign_model_id,
            {"method": "request_state"},
        ).messages
        snapshot = session.snapshot()
        assert all(
            f"anywidget:{foreign_model_id}" not in str(message["data"].get("state", {}))
            for message in snapshot.messages
        )
        assert snapshot.models == {}
        assert snapshot.removed_model_ids == []
    finally:
        session.close()
        foreign_session.close()


def test_descriptor_discovery_failure_restores_owner_and_discards_controller() -> None:
    class BrokenProtocolChild:
        _repr_mimebundle_ = MimeBundleDescriptor(
            _esm="export default { render() {} }",
            autodetect_observer=False,
            follow_changes=False,
        )

        def __init__(self) -> None:
            self.reads = 0

        def _get_anywidget_state(
            self,
            include: set[str] | None,
        ) -> dict[str, object]:
            self.reads += 1
            if self.reads > 1:
                raise RuntimeError("state failed")
            return {"value": 1}

    parent = NestedParentWidget(payload=None)
    session = WidgetSession("instance", parent)
    child = BrokenProtocolChild()
    child_model_id = child._repr_mimebundle_.model_id

    try:
        with pytest.raises(RuntimeError, match="state failed"):
            parent.payload = child

        assert parent.payload is None
        assert set(session.models) == {parent.model_id}
        snapshot = session.snapshot()
        assert all(
            f"anywidget:{child_model_id}" not in str(message)
            for message in snapshot.messages
        )
        assert id(child) not in session._protocol_controllers
    finally:
        session.close()


def test_descriptor_enrollment_failure_discards_controller_and_messages() -> None:
    class BrokenProtocolChild:
        _repr_mimebundle_ = MimeBundleDescriptor(
            _esm="export default { render() {} }",
            autodetect_observer=False,
            follow_changes=False,
        )

        def __init__(self) -> None:
            self.reads = 0

        def _get_anywidget_state(
            self,
            include: set[str] | None,
        ) -> dict[str, object]:
            self.reads += 1
            if self.reads > 2:
                raise RuntimeError("connect state failed")
            return {"value": 1}

    parent = NestedParentWidget(payload=None)
    session = WidgetSession("instance", parent)
    child = BrokenProtocolChild()
    child_model_id = child._repr_mimebundle_.model_id

    try:
        with pytest.raises(RuntimeError, match="connect state failed"):
            parent.payload = child

        assert parent.payload is None
        assert id(child) not in session._protocol_controllers
        snapshot = session.snapshot()
        assert child_model_id not in snapshot.models
        assert all(
            message["modelId"] != child_model_id for message in snapshot.messages
        )
    finally:
        session.close()


def test_enrollment_cleanup_failure_closes_the_session_without_an_orphan_comm() -> None:
    class BrokenEnrollmentChild(ChildWidget):
        close_attempts = 0

        def __init__(self) -> None:
            self.fail_send = False
            super().__init__()
            self.fail_send = True

        def send_state(self, *args: Any, **kwargs: Any) -> None:
            super().send_state(*args, **kwargs)
            if self.fail_send:
                raise RuntimeError("child state failed")

        def close(self) -> None:
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeError("child close failed")
            super().close()

    parent = NestedParentWidget(payload=None)
    session = WidgetSession("instance", parent)
    child = BrokenEnrollmentChild()

    with pytest.raises(ExceptionGroup) as error_info:
        parent.payload = child

    assert "child state failed" in repr(error_info.value)
    assert parent.payload is None
    assert child.close_attempts == 2
    assert child.comm is None
    assert session.snapshot() == SessionSnapshot([], {}, {}, [], None, None)
    session.close()


def test_initialization_error_retains_partial_session_cleanup() -> None:
    class BrokenInitialReadWidget(ChildWidget):
        fail_reads = False
        cleanup_attempts = 0

        def __init__(self) -> None:
            self.arm_failure = False
            super().__init__()
            self.arm_failure = True

        def __getattribute__(self, name: str) -> Any:
            if name == "value" and object.__getattribute__(self, "fail_reads"):
                raise RuntimeError("initial trait read failed")
            return super().__getattribute__(name)

        def send_state(self, *args: Any, **kwargs: Any) -> None:
            super().send_state(*args, **kwargs)
            if self.arm_failure:
                self.fail_reads = True

        def unobserve(self, *args: Any, **kwargs: Any) -> None:
            handler = args[0]
            if isinstance(getattr(handler, "__self__", None), StateContext):
                self.cleanup_attempts += 1
                if self.cleanup_attempts < 3:
                    raise RuntimeError("state observer cleanup failed")
            super().unobserve(*args, **kwargs)

    widget = BrokenInitialReadWidget()

    with pytest.raises(WidgetSessionInitializationError) as error_info:
        WidgetSession("instance", widget, ("value",))

    assert widget.cleanup_attempts == 2
    error_info.value.session.close()
    assert widget.cleanup_attempts == 3
    assert widget.comm is None


def test_removal_state_cleanup_failure_rolls_back_and_can_retry() -> None:
    class TransientStateCleanupChild(ChildWidget):
        attempts = 0

        def unobserve(self, *args: Any, **kwargs: Any) -> None:
            handler = args[0]
            if isinstance(getattr(handler, "__self__", None), StateContext):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("state observer cleanup failed")
            super().unobserve(*args, **kwargs)

    child = TransientStateCleanupChild()
    parent = ParentWidget(child=child)
    session = WidgetSession("instance", parent, lambda _widget: {})

    try:
        with pytest.raises(ExceptionGroup) as error_info:
            parent.child = None
        assert "state observer cleanup failed" in repr(error_info.value)

        assert parent.child is child
        assert child.model_id in session.models
        assert session.snapshot().removed_model_ids == []

        parent.child = None
        snapshot = session.snapshot()
        assert snapshot.removed_model_ids == [child.model_id]
    finally:
        session.close()


def test_removal_graph_cleanup_failure_keeps_observer_for_retry() -> None:
    class TransientGraphCleanupChild(ChildWidget):
        attempts = 0

        def unobserve(self, *args: Any, **kwargs: Any) -> None:
            handler = args[0]
            if getattr(handler, "__name__", None) == "_sync_widget_graph":
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("graph observer cleanup failed")
            super().unobserve(*args, **kwargs)

    child = TransientGraphCleanupChild()
    parent = ParentWidget(child=child)
    session = WidgetSession("instance", parent)

    try:
        with pytest.raises(RuntimeError, match="graph observer cleanup failed"):
            parent.child = None

        assert parent.child is child
        assert id(child) in session._graph_observers

        parent.child = None
        snapshot = session.snapshot()
        assert snapshot.removed_model_ids == [child.model_id]
        assert id(child) not in session._graph_observers
    finally:
        session.close()


def test_replacement_removal_failure_discards_the_new_model() -> None:
    class TransientStateCleanupChild(ChildWidget):
        attempts = 0

        def unobserve(self, *args: Any, **kwargs: Any) -> None:
            handler = args[0]
            if isinstance(getattr(handler, "__self__", None), StateContext):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("state observer cleanup failed")
            super().unobserve(*args, **kwargs)

    old = TransientStateCleanupChild(value=1)
    parent = ParentWidget(child=old)
    session = WidgetSession("instance", parent, lambda _widget: {})
    rejected = ChildWidget(value=2)
    rejected_model_id = rejected.model_id

    try:
        with pytest.raises(ExceptionGroup):
            parent.child = rejected

        assert parent.child is old
        assert set(session.models) == {parent.model_id, old.model_id}
        assert rejected.comm is None
        snapshot = session.snapshot()
        assert rejected_model_id not in snapshot.models
        assert all(
            message["modelId"] != rejected_model_id for message in snapshot.messages
        )
    finally:
        session.close()


def test_partial_removal_acknowledgment_accepts_the_same_retry_set() -> None:
    class TransientCloseChild(ChildWidget):
        def close(self) -> None:
            if getattr(self, "_fail_close", True):
                self._fail_close = False
                raise RuntimeError("detached child close failed")
            super().close()

    first = ChildWidget()
    second = TransientCloseChild()
    parent = NestedParentWidget(payload=[first, second])
    session = WidgetSession("instance", parent)

    try:
        parent.payload = []
        removed_model_ids = session.snapshot().removed_model_ids

        with pytest.raises(ExceptionGroup, match="detached widget models"):
            session.acknowledge_model_removals(removed_model_ids)
        session.acknowledge_model_removals(removed_model_ids)

        assert first.comm is None
        assert second.comm is None
    finally:
        session.close()


def test_acknowledgment_discards_messages_from_the_detached_model() -> None:
    first = ChildWidget(value=1)
    second = ChildWidget(value=2)
    parent = ParentWidget(child=first)
    session = WidgetSession("instance", parent)

    try:
        parent.child = second
        removed_model_ids = session.snapshot().removed_model_ids
        first.value = 3

        session.acknowledge_model_removals(removed_model_ids)

        assert all(
            message["modelId"] != first.model_id
            for message in session.snapshot().messages
        )
    finally:
        session.close()


def test_protocol_container_reused_child_rolls_back_without_closing_session() -> None:
    foreign = ChildWidget(value=4)
    foreign_session = WidgetSession("foreign", foreign)
    original = ChildWidget(value=5)
    container = ProtocolContainer(payload=[original])
    root = NestedParentWidget(payload=[container])
    session = WidgetSession("instance", root)
    foreign_model_id = foreign.model_id

    try:
        with pytest.raises(WidgetInUseError, match="fresh nested widgets"):
            container.payload = [foreign]

        assert container.payload == [original]
        assert foreign.comm is not None
        assert foreign_session.receive(
            foreign_model_id,
            {"method": "request_state"},
        ).messages
        snapshot = session.snapshot()
        assert all(
            f"anywidget:{foreign_model_id}" not in str(message["data"].get("state", {}))
            for message in snapshot.messages
        )
        assert snapshot.models == {}
        assert snapshot.removed_model_ids == []
    finally:
        session.close()
        foreign_session.close()


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

    assert comm is not None
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
    assert session._state_context is None


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
