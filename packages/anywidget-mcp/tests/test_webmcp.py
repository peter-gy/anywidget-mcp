from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from exceptiongroup import ExceptionGroup
import anywidget
import pytest
import traitlets as t
from ipywidgets import Widget

from anywidget_mcp import webmcp


class Counter(anywidget.AnyWidget):
    _esm = "export default { render() {} };"
    value = t.Int(2, min=0, max=20, help="Number of selected points").tag(sync=True)
    doubled = t.Int(4, read_only=True).tag(sync=True)
    secret = t.Unicode("private").tag(sync=True, webmcp=False)

    @t.observe("value")
    def update_doubled(self, change: dict[str, Any]) -> None:
        self.set_trait("doubled", change["new"] * 2)


def descriptor(widget: anywidget.AnyWidget) -> dict[str, Any]:
    return widget.get_state("_webmcp")["_webmcp"]


def request(widget: anywidget.AnyWidget, **arguments: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    assert widget.comm is not None
    with patch.object(
        widget.comm, "send", side_effect=lambda data, **_: messages.append(data)
    ):
        widget._handle_custom_msg(
            {"kind": "anywidget-webmcp", "id": "request", **arguments}, []
        )
    return messages


def test_instrumentation_preserves_host_construction_and_source_serialization() -> None:
    original_callback = Widget._widget_construction_callback
    states = []
    widget = None
    try:
        webmcp.enable()
        try:
            Widget.on_widget_constructed(
                lambda widget: (
                    states.append(widget.get_state(("_esm", "_webmcp")))
                    if isinstance(widget, Counter)
                    else None
                )
            )
            widget = Counter()
            assert states[-1]["_esm"] == widget.get_state("_esm")["_esm"]
            assert states[-1]["_webmcp"] == descriptor(widget)
            assert descriptor(widget)["writable"] == {
                "value": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "description": "Number of selected points",
                }
            }
            assert widget._esm == "export default { render() {} };"
            widget._esm = "export default { render() { return () => {}; } };"
            assert (
                json.loads(
                    widget.get_state("_esm")["_esm"].rsplit(
                        "\nexport default () => instrument(", 1
                    )[1][:-3]
                )
                == widget._esm
            )
        finally:
            webmcp.disable()
        assert widget.get_state("_esm")["_esm"] == widget._esm
        assert widget.comm is not None
    finally:
        Widget.on_widget_constructed(original_callback)
        if widget is not None:
            widget.close()


def test_tools_read_and_update_python_authoritative_state() -> None:
    webmcp.enable()
    try:
        widget = Counter()
        try:
            messages = request(widget, operation="update", state={"value": 7})
            response = messages[-1]["content"]
            assert response == {
                "kind": "anywidget-webmcp-result",
                "id": "request",
                "result": {"state": {"value": 7, "doubled": 14}},
            }
            assert any(
                message.get("method") == "update" and message["state"].get("value") == 7
                for message in messages[:-1]
            )
            assert (
                request(widget, operation="read")[-1]["content"]["result"]
                == response["result"]
            )
        finally:
            widget.close()
    finally:
        webmcp.disable()


@pytest.mark.parametrize(
    "state", [{"value": -1}, {"doubled": 8}, {"secret": "exposed"}, {"unknown": 1}]
)
def test_rejected_tool_updates_leave_the_widget_callable(state: dict[str, Any]) -> None:
    webmcp.enable()
    try:
        widget = Counter()
        try:
            messages = request(widget, operation="update", state=state)
            assert messages[-1]["content"]["error"]
            assert widget.value == 2
            assert widget.doubled == 4
            assert widget.secret == "private"
            assert request(widget, operation="update", state={"value": 3})[-1][
                "content"
            ]["result"] == {"state": {"value": 3, "doubled": 6}}
        finally:
            widget.close()
    finally:
        webmcp.disable()


def test_enable_is_idempotent_for_existing_and_future_instances() -> None:
    assert not webmcp.is_enabled()
    assert webmcp.disable() is None
    first = Counter()
    second = None
    try:
        assert webmcp.enable() is None
        try:
            assert webmcp.is_enabled()
            first_id = descriptor(first)["id"]
            assert webmcp.enable() is None
            assert descriptor(first)["id"] == first_id
            second = Counter()
            assert descriptor(first)["id"] != descriptor(second)["id"]
            request(first, operation="update", state={"value": 9})
            assert second.value == 2
        finally:
            webmcp.disable()
        assert webmcp.disable() is None
        assert not webmcp.is_enabled()
        assert first.get_state("_esm")["_esm"] == first._esm
        assert second.get_state("_esm")["_esm"] == second._esm
        assert request(first, operation="read") == []
        third = Counter()
        assert third.get_state("_esm")["_esm"] == third._esm
        third.close()
    finally:
        first.close()
        if second is not None:
            second.close()


def test_disable_restores_every_widget_when_one_host_delivery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = Counter()
    second = Counter()
    webmcp.enable()
    try:
        with monkeypatch.context() as patch:

            def failed_send(key: Any = None) -> None:
                raise RuntimeError("host disconnected")

            patch.setattr(first, "send_state", failed_send)
            with pytest.raises(ExceptionGroup, match="restore WebMCP"):
                webmcp.disable()
        assert not webmcp.is_enabled()
        for widget in (first, second):
            assert widget.get_state("_esm")["_esm"] == widget._esm
            assert request(widget, operation="read") == []
        webmcp.enable()
        try:
            assert webmcp.is_enabled()
            assert descriptor(first)["writable"]["value"]["type"] == "integer"
        finally:
            webmcp.disable()
    finally:
        webmcp.disable()
        first.close()
        second.close()


def test_failed_start_restores_the_host_dispatcher_and_widget_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = Counter()
    original_dispatcher = Widget._call_widget_constructed
    original_add_traits = Widget.add_traits
    original_send = widget.send_state
    attempts = 0

    def fail_first_send(key: Any = None) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("host disconnected")
        original_send(key)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(widget, "send_state", fail_first_send)
            with pytest.raises(RuntimeError, match="host disconnected"):
                webmcp.enable()
        assert not webmcp.is_enabled()
        assert Widget._call_widget_constructed is original_dispatcher
        assert Widget.add_traits is original_add_traits
        assert widget.get_state("_esm")["_esm"] == widget._esm
        assert request(widget, operation="read") == []
        webmcp.enable()
        try:
            assert webmcp.is_enabled()
            assert descriptor(widget)["title"] == "Counter"
        finally:
            webmcp.disable()
    finally:
        widget.close()


def test_custom_trait_serialization_is_readable_through_its_wire_representation() -> (
    None
):
    class EncodedCounter(Counter):
        encoded = t.Int(5).tag(
            sync=True,
            to_json=lambda value, widget: f"value:{value}",
            from_json=lambda value, widget: int(value.removeprefix("value:")),
        )

    webmcp.enable()
    try:
        widget = EncodedCounter()
        try:
            assert descriptor(widget)["properties"]["encoded"] == {}
            assert "encoded" not in descriptor(widget)["writable"]
            response = request(widget, operation="read")[-1]["content"]
            assert response["result"]["state"]["encoded"] == "value:5"
            rejected = request(widget, operation="update", state={"encoded": 8})
            assert rejected[-1]["content"]["error"]
            assert widget.encoded == 5
        finally:
            widget.close()
    finally:
        webmcp.disable()


def test_source_serializer_survives_instrumentation_hot_reload_and_close() -> None:
    class SerializedCounter(anywidget.AnyWidget):
        _esm = t.Unicode("export default { render() {} };").tag(
            sync=True, to_json=lambda source, widget: source + "\n// serialized"
        )

    widget = SerializedCounter()
    try:
        webmcp.enable()
        try:
            widget._esm = "export default { initialize() {} };"
            source = widget.get_state("_esm")["_esm"]
            arguments = source.rsplit("\nexport default () => instrument(", 1)[1][:-3]
            assert json.loads(arguments) == (widget._esm + "\n// serialized")
        finally:
            webmcp.disable()
        assert widget.get_state("_esm")["_esm"] == widget._esm + "\n// serialized"
    finally:
        widget.close()


def test_enum_with_nonfinite_values_produces_a_json_safe_descriptor() -> None:
    class Status(Counter):
        status = t.Enum([float("inf"), "ready"], default_value="ready").tag(sync=True)

    webmcp.enable()
    try:
        widget = Status()
        try:
            schema = descriptor(widget)
            json.dumps(schema, allow_nan=False)
            assert "status" not in schema["writable"]
            assert (
                request(widget, operation="read")[-1]["content"]["result"]["state"][
                    "status"
                ]
                == "ready"
            )
        finally:
            widget.close()
    finally:
        webmcp.disable()


def test_read_state_bounds_utf8_json_and_summarizes_binary_traits() -> None:
    class DataWidget(anywidget.AnyWidget):
        _esm = "export default { render() {} };"
        binary = t.Bytes(b"x" * 12000).tag(sync=True)
        text = t.Unicode("é" * 12000).tag(sync=True)

    webmcp.enable()
    try:
        widget = DataWidget()
        try:
            state = request(widget, operation="read")[-1]["content"]["result"]["state"]
            assert state["binary"] == {"type": "binary", "bytes": 12000}
            assert state["text"]["characters"] == 12000
            assert state["text"]["truncated"] is True
            encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            assert len(encoded.encode("utf-8")) <= 8000
        finally:
            widget.close()
    finally:
        webmcp.disable()


def test_identical_widget_sources_share_serialized_source_with_distinct_metadata() -> (
    None
):
    webmcp.enable()
    try:
        first = Counter()
        second = Counter()
        try:
            assert first.get_state("_esm") == second.get_state("_esm")
            assert descriptor(first)["id"] != descriptor(second)["id"]
            request(first, operation="update", state={"value": 7})
            assert first.get_state("_esm") == second.get_state("_esm")
            assert second.value == 2
        finally:
            first.close()
            second.close()
    finally:
        webmcp.disable()


def test_stop_clears_metadata_and_preserves_traits_added_during_instrumentation() -> (
    None
):
    widget = Counter()
    try:
        webmcp.enable()
        try:
            widget.add_traits(label=t.Unicode("ready").tag(sync=True))
            first_id = descriptor(widget)["id"]
        finally:
            webmcp.disable()
        assert widget.get_state(("_webmcp", "label")) == {
            "_webmcp": None,
            "label": "ready",
        }
        webmcp.enable()
        try:
            assert descriptor(widget)["id"] != first_id
            assert descriptor(widget)["writable"]["label"] == {"type": "string"}
            messages = request(widget, operation="update", state={"label": "done"})
            assert messages[-1]["content"]["result"]["state"]["label"] == "done"
        finally:
            webmcp.disable()
        assert widget.get_state(("_webmcp", "label")) == {
            "_webmcp": None,
            "label": "done",
        }
        assert widget.keys.count("_webmcp") == 1
    finally:
        widget.close()


def test_stop_preserves_replacement_source_descriptor() -> None:
    widget = Counter()
    try:
        webmcp.enable()
        try:
            widget.add_traits(_esm=t.Unicode("export default { render() {} };"))
            widget._esm = "export default { initialize() {} };"
        finally:
            webmcp.disable()
        assert widget.get_state("_esm")["_esm"] == widget._esm
        assert widget.get_state("_webmcp") == {"_webmcp": None}
        assert request(widget, operation="read") == []
    finally:
        widget.close()


def test_dynamic_traits_refresh_tool_schema_through_native_add_traits() -> None:
    class DynamicCounter(Counter):
        def __init__(self) -> None:
            super().__init__()
            self.add_traits(label=t.Unicode("ready").tag(sync=True))

    original_add_traits = Widget.add_traits
    widget = None
    try:
        webmcp.enable()
        try:
            widget = DynamicCounter()
            assert descriptor(widget)["writable"]["label"] == {"type": "string"}
            messages = request(widget, operation="update", state={"label": "done"})
            assert messages[-1]["content"]["result"]["state"]["label"] == "done"
            source = widget.get_state("_esm")
            widget.add_traits(threshold=t.Float(0.5, min=0, max=1).tag(sync=True))
            assert descriptor(widget)["writable"]["threshold"] == {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            }
            messages = request(widget, operation="update", state={"threshold": 0.75})
            assert messages[-1]["content"]["result"]["state"]["threshold"] == 0.75
            assert widget.get_state("_esm") == source
        finally:
            webmcp.disable()
        assert Widget.add_traits is original_add_traits
        widget.add_traits(status=t.Unicode("closed").tag(sync=True))
        assert widget.get_state(("_webmcp", "status")) == {
            "_webmcp": None,
            "status": "closed",
        }
    finally:
        if widget is not None:
            widget.close()
