from __future__ import annotations

from typing import Any

import pytest
from anywidget import AnyWidget
from traitlets import Dict, Int, List

from anywidget_mcp import StateProjection
from anywidget_mcp._bridge import WidgetSession
from anywidget_mcp._state import DEFAULT_STATE


class ContainerWidget(AnyWidget):
    _esm = "export default { render() {} }"
    payload = Dict().tag(sync=True)
    values = List().tag(sync=True)
    trigger = Int().tag(sync=True)


@pytest.mark.parametrize("state", [DEFAULT_STATE, ("payload", "values", "trigger")])
def test_state_containers_advance_when_native_widget_publishes(state: Any) -> None:
    widget = ContainerWidget(payload={"nested": [1]}, values=[{"value": 2}])
    session = WidgetSession("instance", widget, state)
    try:
        assert session.take_projection() is not None
        widget.payload["nested"].append(3)
        widget.values[0]["value"] = 4
        widget.trigger = 1

        retained = session.current_projection()
        assert retained is not None
        assert retained.state == {
            "payload": {"nested": [1]},
            "values": [{"value": 2}],
            "trigger": 1,
        }

        widget.send_state(["payload", "values"])
        published = session.current_projection()
        assert published is not None
        assert published.state == {
            "payload": {"nested": [1, 3]},
            "values": [{"value": 4}],
            "trigger": 1,
        }

        widget.payload["nested"].append(5)
        widget.values.append(6)
        assert session.current_projection() == published
        widget.payload = {"nested": [1, 3, 5], "published": True}
        widget.values = [{"value": 4}, 6, 7]
        assigned = session.current_projection()
        assert assigned is not None
        assert assigned.state == {
            "payload": {"nested": [1, 3, 5], "published": True},
            "values": [{"value": 4}, 6, 7],
            "trigger": 1,
        }
    finally:
        session.close()


@pytest.mark.parametrize("watch", [None, "trigger"])
@pytest.mark.parametrize("publish", [False, True])
def test_projector_rejects_in_place_synchronized_container_mutation(
    watch: str | None,
    publish: bool,
) -> None:
    widget = ContainerWidget(payload={"nested": [1]})

    def project(current: ContainerWidget) -> dict[str, Any]:
        if current.trigger:
            current.payload["nested"].append(2)
            if publish:
                current.send_state("payload")
        return {"payload": current.payload}

    session = WidgetSession("instance", widget, StateProjection(project, watch=watch))
    try:
        assert session.take_projection() is not None
        widget.trigger = 1
        snapshot = session.snapshot()
        assert snapshot.projection is None
        assert snapshot.projection_error == (
            "State projection callables must not mutate synchronized widget traits"
        )
    finally:
        session.close()


def test_committed_containers_preserve_native_serializer_inputs_and_model_identity() -> (
    None
):
    class Opaque:
        def __deepcopy__(self, memo: dict[int, Any]) -> Any:
            raise AssertionError("Opaque values belong to the native serializer")

    child = ContainerWidget()
    opaque = Opaque()
    binary = memoryview(b"abc")
    shared: list[Any] = [1]
    cycle: list[Any] = []
    cycle.append(cycle)

    def serialize(value: Any, widget: SerializedWidget) -> dict[str, Any]:
        assert value["child"] is child
        assert value["opaque"] is opaque
        assert value["binary"] is binary
        assert type(value["nested"]) is tuple
        assert value["nested"][0] is value["nested"][1]
        assert value["cycle"][0] is value["cycle"]
        return {
            "child": f"IPY_MODEL_{value['child'].model_id}",
            "values": value["nested"][0],
            "scaled": value["nested"][0][0] * widget.factor,
        }

    class SerializedWidget(ContainerWidget):
        factor = 1
        payload = Dict().tag(sync=True, to_json=serialize)

    widget = SerializedWidget(
        payload={
            "child": child,
            "opaque": opaque,
            "binary": binary,
            "nested": (shared, shared),
            "cycle": cycle,
        }
    )
    session = WidgetSession("instance", widget, ("payload", "trigger"))
    try:
        assert session.take_projection() is not None
        shared[0] = 2
        widget.factor = 3
        widget.trigger = 1
        update = session.current_projection()
        assert update is not None
        assert update.state == {
            "payload": {
                "child": f"IPY_MODEL_{child.model_id}",
                "values": [1],
                "scaled": 3,
            },
            "trigger": 1,
        }
        assert child.comm is not None
        session.receive(child.model_id, {"method": "update", "state": {"trigger": 7}})
        assert child.trigger == 7
    finally:
        session.close()


def test_callable_projection_waits_for_native_container_publication() -> None:
    widget = ContainerWidget(payload={"nested": [1]})
    session = WidgetSession(
        "instance", widget, lambda current: {"payload": current.payload}
    )
    try:
        initial = session.take_projection()
        assert initial is not None
        widget.payload["nested"].append(2)
        widget.trigger = 1
        assert session.current_projection() == initial

        widget.send_state("payload")
        published = session.current_projection()
        assert published is not None
        assert published.state == {"payload": {"nested": [1, 2]}}
    finally:
        session.close()
