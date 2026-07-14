from __future__ import annotations

import threading
from typing import Any

import anywidget
import pytest
import traitlets
from anywidget._descriptor import MimeBundleDescriptor
from anywidget.experimental import command

import anywidget_mcp._bridge as bridge
from anywidget_mcp._bridge import SessionSnapshot, WidgetInUseError, WidgetSession
from anywidget_mcp._state import _RefreshStatus


class BinaryWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    payload = traitlets.Bytes(bytes([0, 255])).tag(sync=True)
    size = traitlets.Int(2).tag(sync=True)

    @traitlets.observe("payload")
    def _update_size(self, change: traitlets.Bunch) -> None:
        self.size = len(change.new)


class CommandWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    @command
    def reverse(
        self,
        message: object,
        buffers: list[bytes],
    ) -> tuple[dict[str, object], list[bytes]]:
        return {"seen": message}, [buffer[::-1] for buffer in buffers]


class ChildWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    value = traitlets.Int(7).tag(sync=True)


class ParentWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    child = anywidget.WidgetTrait().tag(sync=True)


class NestedParentWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    payload = traitlets.Any().tag(sync=True)


class ProtocolChild(traitlets.HasTraits):
    _repr_mimebundle_ = MimeBundleDescriptor(
        _esm="export default { render() {} }",
    )

    value = traitlets.Int(3).tag(sync=True)


class ProtocolContainer(traitlets.HasTraits):
    _repr_mimebundle_ = MimeBundleDescriptor(
        _esm="export default { render() {} }",
    )

    payload = traitlets.Any().tag(sync=True)


class ManualProtocolContainer:
    _repr_mimebundle_ = MimeBundleDescriptor(
        _esm="export default { render() {} }",
        autodetect_observer=False,
    )

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def _get_anywidget_state(self, include: set[str] | None) -> dict[str, object]:
        if include is not None and "payload" not in include:
            return {}
        return {"payload": self.payload}


class HybridProtocolContainer(traitlets.HasTraits):
    _repr_mimebundle_ = MimeBundleDescriptor(
        _esm="export default { render() {} }",
    )

    marker = traitlets.Int(0).tag(sync=True)

    def __init__(self, payload: object) -> None:
        super().__init__()
        self.payload = payload

    def _get_anywidget_state(self, include: set[str] | None) -> dict[str, object]:
        state = {"marker": self.marker, "payload": self.payload}
        if include is None:
            return state
        return {name: value for name, value in state.items() if name in include}


class UnhashableWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    __hash__ = None


class EqualWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EqualWidget)

    def __hash__(self) -> int:
        return 1


class PausedNotifyWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    trigger = traitlets.Int(0).tag(sync=True)
    value = traitlets.Int(0).tag(sync=True)

    def __init__(self) -> None:
        super().__init__()
        self.pause_value_notification = False
        self.value_stored = threading.Event()
        self.resume_value_notification = threading.Event()

    def _notify_trait(self, name: str, old_value: Any, new_value: Any) -> None:
        if name == "value" and self.pause_value_notification:
            self.value_stored.set()
            if not self.resume_value_notification.wait(1):
                raise RuntimeError("value notification test did not resume")
        super()._notify_trait(name, old_value, new_value)


class ProjectionWidget(anywidget.AnyWidget):
    _esm = "export default { render() {} }"

    a = traitlets.Int(0).tag(sync=True)
    b = traitlets.Int(0).tag(sync=True)


def test_initial_state_separates_binary_buffers() -> None:
    session = WidgetSession("instance", BinaryWidget())
    model_id = session.root_model_id

    try:
        model = session.models[model_id]

        assert "payload" not in model["state"]
        assert model["bufferPaths"] == [["payload"]]
        assert model["buffers"] == ["AP8="]
    finally:
        session.close()


def test_inbound_binary_update_returns_echo_before_observer_update() -> None:
    widget = BinaryWidget()
    session = WidgetSession("instance", widget)
    model_id = session.root_model_id

    try:
        snapshot = session.receive(
            model_id,
            {
                "method": "update",
                "state": {},
                "buffer_paths": [["payload"]],
            },
            ["AQID"],
        )
        messages = snapshot.messages

        assert widget.payload == bytes([1, 2, 3])
        assert widget.size == 3
        assert messages == [
            {
                "modelId": model_id,
                "data": {
                    "method": "echo_update",
                    "state": {},
                    "buffer_paths": [["payload"]],
                },
                "buffers": ["AQID"],
            },
            {
                "modelId": model_id,
                "data": {
                    "method": "update",
                    "state": {"size": 3},
                    "buffer_paths": [],
                },
                "buffers": [],
            },
        ]
    finally:
        session.close()


def test_custom_message_preserves_command_response_and_buffers() -> None:
    session = WidgetSession("instance", CommandWidget())
    model_id = session.root_model_id

    try:
        snapshot = session.receive(
            model_id,
            {
                "method": "custom",
                "content": {
                    "id": "request-1",
                    "kind": "anywidget-command",
                    "name": "reverse",
                    "msg": {"value": 1},
                },
            },
            ["AP8C"],
        )
        messages = snapshot.messages

        assert messages == [
            {
                "modelId": model_id,
                "data": {
                    "method": "custom",
                    "content": {
                        "id": "request-1",
                        "kind": "anywidget-command-response",
                        "response": {"seen": {"value": 1}},
                    },
                },
                "buffers": ["Av8A"],
            }
        ]
    finally:
        session.close()


def test_widget_trait_includes_child_model_and_reference() -> None:
    child = ChildWidget()
    parent = ParentWidget(child=child)
    session = WidgetSession("instance", parent)
    parent_id = session.root_model_id
    child_id = child.model_id

    try:
        assert set(session.models) == {parent_id, child_id}
        assert session.models[parent_id]["state"]["child"] == f"anywidget:{child_id}"
        assert session.models[child_id]["state"]["value"] == 7
    finally:
        session.close()


def test_nested_synced_state_enrolls_repeated_widget_and_protocol_refs() -> None:
    child = ChildWidget(value=8)
    protocol_child = ProtocolChild(value=5)
    parent = NestedParentWidget(
        payload={
            "items": [
                child,
                {"pair": (protocol_child, child)},
            ]
        }
    )
    session = WidgetSession("instance", parent)
    protocol_id = protocol_child._repr_mimebundle_.model_id

    try:
        models = session.models
        assert set(models) == {parent.model_id, child.model_id, protocol_id}
        assert models[parent.model_id]["state"]["payload"] == {
            "items": [
                f"anywidget:{child.model_id}",
                {
                    "pair": (
                        f"anywidget:{protocol_id}",
                        f"anywidget:{child.model_id}",
                    )
                },
            ]
        }
        assert models[child.model_id]["state"]["value"] == 8
        assert models[protocol_id]["state"]["value"] == 5
    finally:
        session.close()


def test_nested_container_replacement_enrolls_before_snapshot_delivery() -> None:
    first = ChildWidget(value=1)
    parent = NestedParentWidget(payload={"items": [first]})
    session = WidgetSession("instance", parent)
    replacement = ProtocolChild(value=9)
    first_id = first.model_id

    try:
        parent.payload = {"items": [replacement, replacement]}
        replacement_id = replacement._repr_mimebundle_.model_id
        snapshot = session.snapshot()

        assert snapshot.models[replacement_id]["state"]["value"] == 9
        assert snapshot.removed_model_ids == [first_id]
        assert any(
            message["modelId"] == parent.model_id
            and message["data"].get("state", {}).get("payload")
            == {
                "items": [
                    f"anywidget:{replacement_id}",
                    f"anywidget:{replacement_id}",
                ]
            }
            for message in snapshot.messages
        )
    finally:
        session.close()


def test_protocol_child_receives_updates_and_emits_synced_state() -> None:
    child = ProtocolChild(value=2)
    parent = NestedParentWidget(payload=[child])
    session = WidgetSession("instance", parent)
    child_id = child._repr_mimebundle_.model_id

    try:
        snapshot = session.receive(
            child_id,
            {
                "method": "update",
                "state": {"value": 12},
                "buffer_paths": [],
            },
        )

        assert child.value == 12
        assert any(
            message["modelId"] == child_id
            and message["data"].get("state") == {"value": 12}
            for message in snapshot.messages
        )
    finally:
        session.close()


def test_protocol_child_changes_refresh_callable_model_state() -> None:
    child = ProtocolChild(value=4)
    parent = NestedParentWidget(payload=[child])
    session = WidgetSession(
        "instance",
        parent,
        lambda root: {"childValue": root.payload[0].value},
    )

    try:
        initial = session.take_projection()
        assert initial is not None
        assert initial.state == {"childValue": 4}

        child.value = 15
        snapshot = session.snapshot()

        assert snapshot.projection is not None
        assert snapshot.projection.state == {"childValue": 15}
    finally:
        session.close()


def test_manual_protocol_browser_update_refreshes_callable_model_state() -> None:
    child = ManualProtocolContainer(payload=1)
    parent = NestedParentWidget(payload=[child])
    session = WidgetSession(
        "instance",
        parent,
        lambda root: {"childValue": root.payload[0].payload},
    )
    child_id = child._repr_mimebundle_.model_id

    try:
        initial = session.take_projection()
        assert initial is not None
        assert initial.state == {"childValue": 1}

        snapshot = session.receive(
            child_id,
            {
                "method": "update",
                "state": {"payload": 2},
                "buffer_paths": [],
            },
        )

        assert child.payload == 2
        assert snapshot.projection is not None
        assert snapshot.projection.state == {"childValue": 2}
    finally:
        session.close()


def test_protocol_container_change_updates_nested_model_membership() -> None:
    first = ChildWidget(value=1)
    container = ProtocolContainer(payload={"items": [first]})
    root = NestedParentWidget(payload=[container])
    session = WidgetSession("instance", root)
    first_id = first.model_id
    container_id = container._repr_mimebundle_.model_id
    second = ChildWidget(value=2)

    try:
        container.payload = {"items": [(second, second)]}
        snapshot = session.snapshot()

        assert snapshot.models[second.model_id]["state"]["value"] == 2
        assert snapshot.removed_model_ids == [first_id]
        assert any(
            message["modelId"] == container_id
            and message["data"].get("state", {}).get("payload")
            == {
                "items": [
                    (
                        f"anywidget:{second.model_id}",
                        f"anywidget:{second.model_id}",
                    )
                ]
            }
            for message in snapshot.messages
        )
    finally:
        session.close()


def test_protocol_state_send_rescans_nested_models_without_traitlets() -> None:
    first = ChildWidget(value=1)
    container = ManualProtocolContainer(payload=[first])
    root = NestedParentWidget(payload=[container])
    session = WidgetSession("instance", root)
    first_id = first.model_id
    second = ChildWidget(value=6)

    try:
        container.payload = {"child": second}
        container._repr_mimebundle_.send_state("payload")
        snapshot = session.snapshot()

        assert snapshot.models[second.model_id]["state"]["value"] == 6
        assert snapshot.removed_model_ids == [first_id]
    finally:
        session.close()


def test_protocol_explicit_state_send_rescans_graph_and_projection() -> None:
    first = ChildWidget(value=1)
    container = HybridProtocolContainer(payload=[first])
    root = NestedParentWidget(payload=[container])
    session = WidgetSession(
        "instance",
        root,
        lambda current: {"child": current.payload[0].payload[0].model_id},
    )
    first_model_id = first.model_id
    container_id = container._repr_mimebundle_.model_id
    second = ChildWidget(value=6)

    try:
        assert session.take_projection() is not None
        container.payload = [second]
        container._repr_mimebundle_.send_state("payload")
        snapshot = session.snapshot()

        assert snapshot.models[second.model_id]["state"]["value"] == 6
        assert snapshot.removed_model_ids == [first_model_id]
        assert snapshot.projection is not None
        assert snapshot.projection.state == {"child": second.model_id}
        assert any(
            message["modelId"] == container_id
            and message["data"].get("state", {}).get("payload")
            == [f"anywidget:{second.model_id}"]
            for message in snapshot.messages
        )
    finally:
        session.close()


def test_anywidget_explicit_state_send_rescans_graph_and_projection() -> None:
    first = ChildWidget(value=1)
    root = NestedParentWidget(payload=[first])
    session = WidgetSession(
        "instance",
        root,
        lambda current: {"child": current.payload[0].model_id},
    )
    first_model_id = first.model_id
    second = ChildWidget(value=6)

    try:
        assert session.take_projection() is not None
        root.payload[0] = second
        root.send_state("payload")
        snapshot = session.snapshot()

        assert snapshot.models[second.model_id]["state"]["value"] == 6
        assert snapshot.removed_model_ids == [first_model_id]
        assert snapshot.projection is not None
        assert snapshot.projection.state == {"child": second.model_id}
        assert any(
            message["modelId"] == root.model_id
            and message["data"].get("state", {}).get("payload")
            == [f"anywidget:{second.model_id}"]
            for message in snapshot.messages
        )
    finally:
        session.close()


def test_widget_trait_replacement_adds_new_model_and_disposes_detached_child() -> None:
    first = ChildWidget(value=7)
    parent = ParentWidget(child=first)
    session = WidgetSession("instance", parent)
    second = ChildWidget(value=9)
    first_model_id = first.model_id

    try:
        parent.child = second
        snapshot = session.snapshot()

        assert snapshot.models[second.model_id]["state"]["value"] == 9
        assert snapshot.removed_model_ids == [first_model_id]
        assert first.comm is None
        assert any(
            message["modelId"] == parent.model_id
            and message["data"].get("state", {}).get("child")
            == f"anywidget:{second.model_id}"
            for message in snapshot.messages
        )

        response = session.receive(second.model_id, {"method": "request_state"})
        assert any(
            message["modelId"] == second.model_id for message in response.messages
        )
    finally:
        session.close()


def test_snapshot_keeps_widget_trait_notification_components_together() -> None:
    notification_paused = threading.Event()
    resume_notification = threading.Event()
    snapshot_waiting = threading.Event()
    mutation_done = threading.Event()
    snapshot_done = threading.Event()
    errors: list[BaseException] = []
    snapshots: list[SessionSnapshot] = []
    first = ChildWidget(value=7)
    second = ChildWidget(value=9)
    parent = ParentWidget(child=first)

    def pause_notification(_change: object) -> None:
        notification_paused.set()
        if not resume_notification.wait(1):
            raise RuntimeError("notification test did not resume")

    def project(widget: anywidget.AnyWidget) -> dict[str, str]:
        assert isinstance(widget, ParentWidget)
        return {"child": widget.child.model_id}

    parent.observe(pause_notification, names="child")
    session = WidgetSession("instance", parent, project)
    first_model_id = first.model_id
    original_wait = session._wait_for_notifications

    def wait_for_notifications(action: str) -> None:
        snapshot_waiting.set()
        original_wait(action)

    setattr(session, "_wait_for_notifications", wait_for_notifications)

    def replace_child() -> None:
        try:
            parent.child = second
        except BaseException as error:
            errors.append(error)
        finally:
            mutation_done.set()

    def take_snapshot() -> None:
        try:
            snapshots.append(session.snapshot())
        except BaseException as error:
            errors.append(error)
        finally:
            snapshot_done.set()

    try:
        assert session.take_projection() is not None
        mutation = threading.Thread(target=replace_child)
        mutation.start()
        assert notification_paused.wait(1)
        capture = threading.Thread(target=take_snapshot)
        capture.start()
        assert snapshot_waiting.wait(1)
        resume_notification.set()
        assert mutation_done.wait(1)
        assert snapshot_done.wait(1)
        mutation.join()
        capture.join()
        setattr(session, "_wait_for_notifications", original_wait)

        assert errors == []
        snapshot = snapshots[0]
        assert snapshot.models[second.model_id]["state"]["value"] == 9
        assert snapshot.removed_model_ids == [first_model_id]
        assert snapshot.projection is not None
        assert snapshot.projection.state == {"child": second.model_id}
        assert any(
            message["modelId"] == parent.model_id
            and message["data"].get("state", {}).get("child")
            == f"anywidget:{second.model_id}"
            for message in snapshot.messages
        )
        following = session.snapshot()
        assert following.messages == []
        assert following.models == {}
        assert following.removed_model_ids == []
        assert following.projection is None
    finally:
        resume_notification.set()
        setattr(session, "_wait_for_notifications", original_wait)
        session.close()


def test_snapshot_keeps_protocol_trait_notification_components_together() -> None:
    notification_paused = threading.Event()
    resume_notification = threading.Event()
    snapshot_waiting = threading.Event()
    snapshot_done = threading.Event()
    errors: list[BaseException] = []
    snapshots: list[SessionSnapshot] = []
    first = ChildWidget(value=7)
    second = ChildWidget(value=9)
    container = ProtocolContainer(payload=[first])
    root = NestedParentWidget(payload=[container])

    def pause_notification(_change: object) -> None:
        notification_paused.set()
        if not resume_notification.wait(1):
            raise RuntimeError("notification test did not resume")

    container.observe(pause_notification, names="payload")
    session = WidgetSession(
        "instance",
        root,
        lambda current: {"child": current.payload[0].payload[0].model_id},
    )
    first_model_id = first.model_id
    original_wait = session._wait_for_notifications

    def wait_for_notifications(action: str) -> None:
        snapshot_waiting.set()
        original_wait(action)

    setattr(session, "_wait_for_notifications", wait_for_notifications)

    def replace_child() -> None:
        try:
            container.payload = [second]
        except BaseException as error:
            errors.append(error)

    def take_snapshot() -> None:
        try:
            snapshots.append(session.snapshot())
        except BaseException as error:
            errors.append(error)
        finally:
            snapshot_done.set()

    try:
        assert session.take_projection() is not None
        mutation = threading.Thread(target=replace_child)
        mutation.start()
        assert notification_paused.wait(1)
        capture = threading.Thread(target=take_snapshot)
        capture.start()
        assert snapshot_waiting.wait(1)
        assert not snapshot_done.is_set()
        resume_notification.set()
        mutation.join()
        capture.join()

        assert errors == []
        snapshot = snapshots[0]
        assert snapshot.models[second.model_id]["state"]["value"] == 9
        assert snapshot.removed_model_ids == [first_model_id]
        assert snapshot.projection is not None
        assert snapshot.projection.state == {"child": second.model_id}
    finally:
        resume_notification.set()
        setattr(session, "_wait_for_notifications", original_wait)
        session.close()


def test_snapshot_projects_last_notified_values_during_pre_notify_write() -> None:
    widget = PausedNotifyWidget()
    session = WidgetSession("instance", widget, ("trigger", "value"))
    errors: list[BaseException] = []

    def change_value() -> None:
        try:
            widget.value = 8
        except BaseException as error:
            errors.append(error)

    try:
        assert session.take_projection() is not None
        widget.pause_value_notification = True
        mutation = threading.Thread(target=change_value)
        mutation.start()
        assert widget.value_stored.wait(1)

        widget.trigger = 1
        first = session.snapshot()

        assert first.projection is not None
        assert first.projection.state == {"trigger": 1, "value": 0}
        assert [message["data"]["state"] for message in first.messages] == [
            {"trigger": 1}
        ]

        widget.resume_value_notification.set()
        mutation.join()
        second = session.snapshot()

        assert errors == []
        assert second.projection is not None
        assert second.projection.state == {"trigger": 1, "value": 8}
        assert [message["data"]["state"] for message in second.messages] == [
            {"value": 8}
        ]
    finally:
        widget.resume_value_notification.set()
        session.close()


def test_snapshot_keeps_prior_dirty_projection_before_value_notification() -> None:
    widget = PausedNotifyWidget()
    session = WidgetSession("instance", widget, ("trigger", "value"))
    errors: list[BaseException] = []

    def change_value() -> None:
        try:
            widget.value = 8
        except BaseException as error:
            errors.append(error)

    try:
        assert session.take_projection() is not None
        widget.trigger = 1
        widget.pause_value_notification = True
        mutation = threading.Thread(target=change_value)
        mutation.start()
        assert widget.value_stored.wait(1)

        first = session.snapshot()

        assert first.projection is not None
        assert first.projection.state == {"trigger": 1, "value": 0}
        assert [message["data"]["state"] for message in first.messages] == [
            {"trigger": 1}
        ]

        widget.resume_value_notification.set()
        mutation.join()
        second = session.snapshot()

        assert errors == []
        assert second.projection is not None
        assert second.projection.state == {"trigger": 1, "value": 8}
        assert [message["data"]["state"] for message in second.messages] == [
            {"value": 8}
        ]
    finally:
        widget.resume_value_notification.set()
        session.close()


def test_custom_projection_cannot_commit_a_pre_notify_value() -> None:
    widget = PausedNotifyWidget()
    projection_started = threading.Event()
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
    snapshots: list[SessionSnapshot] = []
    errors: list[BaseException] = []

    def take_snapshot() -> None:
        try:
            snapshots.append(session.snapshot())
        except BaseException as error:
            errors.append(error)

    def change_value() -> None:
        try:
            widget.value = 8
        except BaseException as error:
            errors.append(error)

    try:
        assert session.take_projection() is not None
        widget.trigger = 1
        widget.pause_value_notification = True
        capture = threading.Thread(target=take_snapshot)
        capture.start()
        assert projection_started.wait(1)
        mutation = threading.Thread(target=change_value)
        mutation.start()
        assert widget.value_stored.wait(1)
        capture.join()

        assert errors == []
        assert len(snapshots) == 1
        first = snapshots[0]
        assert first.projection is None
        assert first.projection_error is None
        assert [message["data"]["state"] for message in first.messages] == [
            {"trigger": 1}
        ]

        widget.resume_value_notification.set()
        mutation.join()
        second = session.snapshot()

        assert second.projection is not None
        assert second.projection.state == {"trigger": 1, "value": 8}
        assert [message["data"]["state"] for message in second.messages] == [
            {"value": 8}
        ]
    finally:
        widget.resume_value_notification.set()
        session.close()


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
        assert session.snapshot() == SessionSnapshot([], {}, [], None, None)
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
        assert session.snapshot() == SessionSnapshot([], {}, [], None, None)

        release_projection.set()
        assert snapshot_done.wait(1)
        capture.join()

        assert errors == []
        assert snapshots == [SessionSnapshot([], {}, [], None, None)]
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
        assert session.snapshot() == SessionSnapshot([], {}, [], None, None)
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
    assert session.snapshot() == SessionSnapshot([], {}, [], None, None)
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
        assert session.receive(
            second.model_id,
            {"method": "request_state"},
        ).messages
        assert first_comm is not None
        with pytest.raises(RuntimeError, match="widget session is closed"):
            first_comm.receive({"method": "request_state"})
    finally:
        session.close()
        first.close()

    assert replacement_closed


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
            raise RuntimeError("widget close failed")

    widget = BrokenWidget()
    session = WidgetSession("instance", widget)
    comm = widget.comm

    with pytest.raises(ExceptionGroup, match="Failed to close widget session"):
        session.close()

    assert comm is not None
    with pytest.raises(RuntimeError, match="widget session is closed"):
        comm.receive({"method": "request_state"})
    session.close()


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
