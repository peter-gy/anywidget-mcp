from __future__ import annotations

import threading

import anywidget
import pytest

import anywidget_mcp._bridge as bridge
from anywidget_mcp._bridge import SessionSnapshot, WidgetSession

from .bridge_test_widgets import (
    BinaryWidget,
    ChildWidget,
    CommandWidget,
    HotSourceWidget,
    HybridProtocolContainer,
    ManualProtocolContainer,
    NestedParentWidget,
    ParentWidget,
    PausedNotifyWidget,
    ProtocolChild,
    ProtocolContainer,
    SharedSourceChild,
)


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


def test_reused_child_cleanup_failure_retries_fresh_sibling_close() -> None:
    class TransientCloseChild(ChildWidget):
        close_attempts = 0

        def close(self) -> None:
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeError("fresh child close failed")
            super().close()

    foreign = ChildWidget(value=1)
    foreign_session = WidgetSession("foreign", foreign)
    original = ChildWidget(value=2)
    parent = NestedParentWidget(payload=[original])
    session = WidgetSession("instance", parent)
    fresh = TransientCloseChild(value=3)

    try:
        with pytest.raises(ExceptionGroup) as error_info:
            parent.payload = [foreign, fresh]

        assert "fresh child close failed" in repr(error_info.value)
        assert parent.payload == [original]
        assert fresh.close_attempts == 2
        assert fresh.comm is None
        assert foreign_session.receive(
            foreign.model_id,
            {"method": "request_state"},
        ).messages
        assert session.snapshot() == SessionSnapshot([], {}, {}, [], None, None)
    finally:
        session.close()
        foreign_session.close()


def test_asset_registry_retains_live_sources_and_the_latest_snapshot() -> None:
    widget = HotSourceWidget()
    session = WidgetSession("instance", widget)

    try:
        launch = session.launch_snapshot()
        initial_id = launch.models[widget.model_id]["sourceRefs"]["_esm"]

        widget._esm = "export default { render() { return 'middle'; } }"
        widget._esm = "export default { render() { return 'current'; } }"
        update = session.snapshot()
        update_ids = [
            message["sourceRefs"]["_esm"]
            for message in update.messages
            if "sourceRefs" in message
        ]

        assert len(update_ids) == 2
        assert set(update.asset_manifest) == set(update_ids)
        assert session.asset_contents(update_ids)[update_ids[0]]["text"].endswith(
            "return 'middle'; } }"
        )
        assert session.asset_contents(update_ids)[update_ids[1]]["text"].endswith(
            "return 'current'; } }"
        )
        with pytest.raises(KeyError, match="Unknown widget asset"):
            session.asset_contents([initial_id])

        following = session.snapshot()
        assert following.asset_manifest == {}
        with pytest.raises(KeyError, match="Unknown widget asset"):
            session.asset_contents([update_ids[0]])
        assert session.asset_contents([update_ids[1]])[update_ids[1]]["text"].endswith(
            "return 'current'; } }"
        )

        widget._esm = "export default { render() { return 'middle'; } }"
        repeated = session.snapshot()
        assert set(repeated.asset_manifest) == {update_ids[0]}
        assert session.asset_contents([update_ids[0]])[update_ids[0]]["text"].endswith(
            "return 'middle'; } }"
        )
    finally:
        session.close()


def test_failed_asset_snapshot_preserves_the_committed_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = HotSourceWidget()
    session = WidgetSession("instance", widget)
    original_asset_id = bridge._asset_id

    try:
        launch = session.launch_snapshot()
        initial_id = launch.models[widget.model_id]["sourceRefs"]["_esm"]

        def fail_new_source(kind: str, source: str) -> str:
            if "broken" in source:
                raise RuntimeError("asset hashing failed")
            return original_asset_id(kind, source)

        monkeypatch.setattr(bridge, "_asset_id", fail_new_source)
        widget._esm = "export default { render() { return 'broken'; } }"

        with pytest.raises(RuntimeError, match="asset hashing failed"):
            session.snapshot()
        assert session.asset_contents([initial_id])[initial_id]["text"].endswith(
            "return 'initial'; } }"
        )
    finally:
        session.close()


def test_asset_registry_releases_sources_after_the_last_live_model_detaches() -> None:
    first = SharedSourceChild()
    second = SharedSourceChild()
    root = NestedParentWidget(payload=[first, second])
    session = WidgetSession("instance", root)

    try:
        launch = session.launch_snapshot()
        first_id = launch.models[first.model_id]["sourceRefs"]["_esm"]
        second_id = launch.models[second.model_id]["sourceRefs"]["_esm"]
        assert first_id == second_id

        root.payload = [second]
        session.snapshot()
        assert session.asset_contents([first_id])[first_id]["text"].endswith(
            "return 'shared'; } }"
        )

        root.payload = []
        session.snapshot()
        with pytest.raises(KeyError, match="Unknown widget asset"):
            session.asset_contents([first_id])
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
        assert first.comm is not None
        assert any(
            message["modelId"] == parent.model_id
            and message["data"].get("state", {}).get("child")
            == f"anywidget:{second.model_id}"
            for message in snapshot.messages
        )

        session.acknowledge_model_removals([first_model_id])
        assert first.comm is None

        response = session.receive(second.model_id, {"method": "request_state"})
        assert any(
            message["modelId"] == second.model_id for message in response.messages
        )
    finally:
        session.close()


def test_detached_child_accepts_browser_update_until_removal_snapshot() -> None:
    first = ChildWidget(value=7)
    parent = ParentWidget(child=first)
    session = WidgetSession("instance", parent)
    second = ChildWidget(value=9)
    first_model_id = first.model_id
    first_comm = first.comm

    try:
        session.launch_snapshot()
        parent.child = second

        assert first.comm is first_comm
        snapshot = session.receive(
            first_model_id,
            {
                "method": "update",
                "state": {"value": 11},
                "buffer_paths": [],
            },
        )

        assert first.value == 11
        assert snapshot.removed_model_ids == [first_model_id]
        assert any(
            message["modelId"] == first_model_id
            and message["data"].get("state") == {"value": 11}
            for message in snapshot.messages
        )
        assert first.comm is first_comm
        with pytest.raises(KeyError, match="not awaiting acknowledgment"):
            session.acknowledge_model_removals(["unknown-model"])

        session.acknowledge_model_removals([first_model_id])
        assert first.comm is None
        with pytest.raises(KeyError, match="Unknown widget model"):
            session.receive(first_model_id, {"method": "request_state"})
    finally:
        session.close()


def test_close_disposes_child_detached_before_removal_snapshot() -> None:
    first = ChildWidget(value=7)
    parent = ParentWidget(child=first)
    session = WidgetSession("instance", parent)
    second = ChildWidget(value=9)

    parent.child = second
    session.close()

    assert first.comm is None
    assert second.comm is None
    assert parent.comm is None


def test_close_disposes_child_awaiting_removal_acknowledgment() -> None:
    first = ChildWidget(value=7)
    parent = ParentWidget(child=first)
    session = WidgetSession("instance", parent)
    second = ChildWidget(value=9)

    parent.child = second
    session.snapshot()
    session.close()

    assert first.comm is None
    assert second.comm is None
    assert parent.comm is None


def test_launch_snapshot_finalizes_models_never_exposed_to_the_browser() -> None:
    first = ChildWidget(value=7)
    parent = ParentWidget(child=first)
    session = WidgetSession("instance", parent)
    second = ChildWidget(value=9)
    first_model_id = first.model_id

    try:
        parent.child = second
        launch = session.launch_snapshot()

        assert first_model_id not in launch.models
        assert launch.removed_model_ids == []
        assert first.comm is None
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
