from __future__ import annotations

import threading
from typing import Any

import anywidget
import pytest
from anywidget._descriptor import MimeBundleDescriptor

import anywidget_mcp._bridge as bridge
from anywidget_mcp._bridge import (
    SessionSnapshot,
    WidgetInUseError,
    WidgetSession,
)
from anywidget_mcp._state import StateContext

from ._server_support import leaf_error_messages
from .bridge_test_widgets import (
    ChildWidget,
    NestedParentWidget,
    ParentWidget,
    ProjectionWidget,
    ProtocolContainer,
)


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
        assert session.snapshot().projection_error is None
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
    model_id = session.root_model_id

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
        with pytest.raises(RuntimeError, match="widget session is closed"):
            session.receive(model_id, {"method": "request_state"})

        release_projection.set()
        assert snapshot_done.wait(1)
        capture.join()

        assert errors == []
        assert len(snapshots) == 1
        assert snapshots[0].projection is None
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


def test_detached_protocol_model_stops_accepting_messages_after_acknowledgment() -> (
    None
):
    first = ProtocolContainer(payload={})
    parent = NestedParentWidget(payload=first)
    session = WidgetSession("instance", parent)
    second = ProtocolContainer(payload={})
    first_model_id = first._repr_mimebundle_.model_id
    second_model_id = second._repr_mimebundle_.model_id

    try:
        parent.payload = second
        snapshot = session.snapshot()
        assert session.receive(
            first_model_id,
            {"method": "request_state"},
        ).messages
        session.acknowledge_model_removals(snapshot.removed_model_ids)

        with pytest.raises(KeyError, match="Unknown widget model"):
            session.receive(first_model_id, {"method": "request_state"})
        assert session.receive(
            second_model_id,
            {"method": "request_state"},
        ).messages
    finally:
        session.close()


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
        assert [message["data"]["state"] for message in snapshot.messages] == [
            {"payload": None}
        ]
        with pytest.raises(KeyError, match="Unknown widget model"):
            session.receive(child_model_id, {"method": "request_state"})
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
    parent_model_id = parent.model_id
    child = BrokenEnrollmentChild()

    with pytest.raises(ExceptionGroup) as error_info:
        parent.payload = child

    assert "child state failed" in leaf_error_messages(error_info.value)
    assert parent.payload is None
    assert child.close_attempts == 2
    assert child.comm is None
    with pytest.raises(RuntimeError, match="widget session is closed"):
        session.receive(parent_model_id, {"method": "request_state"})
    session.close()


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
        assert "state observer cleanup failed" in leaf_error_messages(error_info.value)

        assert parent.child is child
        assert child.model_id in session.models
        assert session.snapshot().removed_model_ids == []

        parent.child = None
        snapshot = session.snapshot()
        assert snapshot.removed_model_ids == [child.model_id]
        assert child.attempts == 2
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

        parent.child = None
        snapshot = session.snapshot()
        assert snapshot.removed_model_ids == [child.model_id]
        assert child.attempts == 2
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

    try:
        with pytest.raises(ExceptionGroup):
            parent.child = rejected

        assert parent.child is old
        assert set(session.models) == {parent.model_id, old.model_id}
        assert rejected.comm is None
        snapshot = session.snapshot()
        assert [message["data"]["state"] for message in snapshot.messages] == [
            {"child": f"anywidget:{old.model_id}"}
        ]
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
        second.value = 4

        assert [
            message["data"]["state"] for message in session.snapshot().messages
        ] == [{"value": 4}]
    finally:
        session.close()


def test_protocol_container_restores_the_original_and_keeps_foreign_session_live() -> (
    None
):
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
        expected_state = {"payload": [f"anywidget:{original.model_id}"]}
        assert snapshot.messages
        assert all(
            message["data"].get("state") == expected_state
            for message in snapshot.messages
        )
        assert snapshot.models == {}
        assert snapshot.removed_model_ids == []
    finally:
        session.close()
        foreign_session.close()
