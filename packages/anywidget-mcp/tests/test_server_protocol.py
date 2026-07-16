from __future__ import annotations

from typing import Any

import pytest
from anywidget import AnyWidget
from mcp.types import TextContent

from anywidget_mcp import AnyWidgetMCP
from anywidget_mcp._bridge import SessionSnapshot, WidgetSession

from ._server_support import (
    CounterWidget,
    ParentWidget,
    ValidatedCounterWidget,
    connected,
)


@pytest.mark.anyio
async def test_state_modes_control_initial_model_visibility() -> None:
    selected = AnyWidgetMCP("selected")

    @selected.widget(state=("value",))
    def selected_counter() -> CounterWidget:
        return CounterWidget(value=4)

    async with connected(selected) as client:
        selected_result = await client.call_tool("selected_counter", {})

    assert selected_result.structuredContent == {
        "tool": "selected_counter",
        "state": {"value": 4},
    }
    assert selected_result.meta is not None
    assert selected_result.meta["anywidget"]["context"]["state"] == {"value": 4}

    hidden = AnyWidgetMCP("hidden")

    @hidden.widget(state=None)
    def hidden_counter() -> CounterWidget:
        return CounterWidget(value=5)

    async with connected(hidden) as client:
        hidden_result = await client.call_tool("hidden_counter", {})

    assert hidden_result.structuredContent == {"tool": "hidden_counter"}
    assert hidden_result.content == [
        TextContent(type="text", text="Opened Hidden Counter.")
    ]
    assert hidden_result.meta is not None
    assert "context" not in hidden_result.meta["anywidget"]

    custom = AnyWidgetMCP("custom")

    @custom.widget(state=lambda widget: {"answer": widget.doubled})
    def custom_counter() -> CounterWidget:
        return CounterWidget(value=6)

    async with connected(custom) as client:
        custom_result = await client.call_tool("custom_counter", {})

    assert custom_result.structuredContent == {
        "tool": "custom_counter",
        "state": {"answer": 12},
    }


@pytest.mark.anyio
async def test_unknown_selected_state_trait_is_a_tool_error() -> None:
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []

    @server.widget(state=("missing",))
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    async with connected(server) as client:
        result = await client.call_tool("counter", {})

    assert result.isError is True
    assert isinstance(result.content[0], TextContent)
    assert "Unknown state trait for CounterWidget: missing" in result.content[0].text
    assert created[0].comm is None


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["non_mapping", "raising"])
async def test_initial_projection_failure_closes_the_root_and_child(
    failure: str,
) -> None:
    server = AnyWidgetMCP("test")
    created: list[tuple[ParentWidget, CounterWidget]] = []

    def project(_widget: AnyWidget) -> Any:
        if failure == "raising":
            raise RuntimeError("projection exploded")
        return ["not", "a", "mapping"]

    @server.widget(state=project)
    def parent() -> ParentWidget:
        child = CounterWidget()
        root = ParentWidget(child=child)
        created.append((root, child))
        return root

    async with connected(server) as client:
        result = await client.call_tool("parent", {})

    assert result.isError is True
    assert isinstance(result.content[0], TextContent)
    assert "Widget state projection failed" in result.content[0].text
    if failure == "raising":
        assert "projection exploded" in result.content[0].text
    else:
        assert "must return a mapping" in result.content[0].text
    assert created[0][0].comm is None
    assert created[0][1].comm is None


@pytest.mark.anyio
async def test_comm_applies_browser_update_and_returns_observer_update() -> None:
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []

    @server.widget
    def counter(value: int = 1) -> CounterWidget:
        widget = CounterWidget(value=value)
        created.append(widget)
        return widget

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]
        result = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": payload["instanceId"],
                "model_id": payload["rootModelId"],
                "operation_id": "apply-browser-update",
                "data": {
                    "method": "update",
                    "state": {"value": 7},
                    "buffer_paths": [],
                },
            },
        )

    assert created[0].value == 7
    assert created[0].doubled == 14
    assert result.structuredContent is None
    assert result.meta is not None
    messages = result.meta["anywidget"]["messages"]
    assert any(
        message["data"]["method"] == "echo_update"
        and message["data"]["state"] == {"value": 7}
        for message in messages
    )
    context = result.meta["anywidget"]["context"]
    assert context["version"] == 2
    assert context["state"]["value"] == 7
    assert context["state"]["doubled"] == 14
    assert any(
        message["data"]["method"] == "update"
        and message["data"]["state"] == {"doubled": 14}
        for message in messages
    )


@pytest.mark.anyio
async def test_comm_operation_id_replays_the_complete_response() -> None:
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []

    @server.widget
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]
        arguments = {
            "instance_id": payload["instanceId"],
            "model_id": payload["rootModelId"],
            "operation_id": "operation-1",
            "data": {
                "method": "update",
                "state": {"value": 7},
                "buffer_paths": [],
            },
        }
        first = await client.call_tool("anywidget_comm", arguments)
        replay = await client.call_tool("anywidget_comm", arguments)
        idle = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": payload["instanceId"],
                "operation_id": "poll-after-comm-replay",
            },
        )

    assert created[0].value == 7
    assert created[0].doubled == 14
    assert replay.meta == first.meta
    assert idle.meta is not None
    assert idle.meta["anywidget"] == {
        "protocolVersion": 1,
        "assetManifest": {},
        "messages": [],
    }


@pytest.mark.anyio
async def test_comm_operation_id_rejects_another_request() -> None:
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []

    @server.widget
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]
        common = {
            "instance_id": payload["instanceId"],
            "model_id": payload["rootModelId"],
            "operation_id": "operation-1",
        }
        first = await client.call_tool(
            "anywidget_comm",
            {
                **common,
                "data": {"method": "update", "state": {"value": 7}},
            },
        )
        reused = await client.call_tool(
            "anywidget_comm",
            {
                **common,
                "data": {"method": "update", "state": {"value": 8}},
            },
        )

    assert first.isError is False
    assert reused.isError is True
    assert created[0].value == 7
    assert isinstance(reused.content[0], TextContent)
    assert "was reused for another comm request" in reused.content[0].text


@pytest.mark.anyio
async def test_comm_operation_id_replays_handler_failure() -> None:
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []
    attempts = 0

    @server.widget
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    def fail_after_update(_message: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        raise TypeError("browser update failed")

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]
        comm = created[0].comm
        assert comm is not None
        comm.on_msg(fail_after_update)
        arguments = {
            "instance_id": payload["instanceId"],
            "model_id": payload["rootModelId"],
            "operation_id": "failing-update",
            "data": {"method": "update", "state": {"value": 7}},
        }
        first = await client.call_tool("anywidget_comm", arguments)
        replay = await client.call_tool("anywidget_comm", arguments)
        mismatched = await client.call_tool(
            "anywidget_comm",
            {
                **arguments,
                "data": {"method": "update", "state": {"value": 8}},
            },
        )

    assert attempts == 1
    assert first.isError is True
    assert replay.isError is True
    assert first.content == replay.content
    assert mismatched.isError is True
    assert isinstance(mismatched.content[0], TextContent)
    assert "was reused for another comm request" in mismatched.content[0].text


@pytest.mark.anyio
async def test_comm_projects_python_validated_state() -> None:
    server = AnyWidgetMCP("test")
    server.widget(ValidatedCounterWidget)

    async with connected(server) as client:
        launch = await client.call_tool("validated_counter_widget", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]
        result = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": payload["instanceId"],
                "model_id": payload["rootModelId"],
                "operation_id": "validated-state-update",
                "data": {
                    "method": "update",
                    "state": {"value": 99},
                    "buffer_paths": [],
                },
            },
        )

    assert result.meta is not None
    assert result.meta["anywidget"]["context"] == {
        "version": 2,
        "tool": "validated_counter_widget",
        "state": {"value": 10},
    }


@pytest.mark.anyio
async def test_poll_operation_id_replays_the_complete_response() -> None:
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []

    @server.widget
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        instance_id = launch.meta["anywidget"]["instanceId"]
        created[0].doubled = 22
        arguments = {
            "instance_id": instance_id,
            "operation_id": "poll-operation-1",
        }
        first = await client.call_tool("anywidget_poll", arguments)

        created[0].doubled = 44
        replay = await client.call_tool("anywidget_poll", arguments)
        mismatched = await client.call_tool(
            "anywidget_poll",
            {
                **arguments,
                "acknowledged_model_ids": ["different-model"],
            },
        )
        next_poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": instance_id,
                "operation_id": "poll-operation-2",
            },
        )

    assert replay.meta == first.meta
    assert mismatched.isError is True
    assert isinstance(mismatched.content[0], TextContent)
    assert "was reused for another poll request" in mismatched.content[0].text
    assert first.meta is not None
    assert any(
        message["data"]["state"] == {"doubled": 22}
        for message in first.meta["anywidget"]["messages"]
    )
    assert next_poll.meta is not None
    assert any(
        message["data"]["state"] == {"doubled": 44}
        for message in next_poll.meta["anywidget"]["messages"]
    )


@pytest.mark.anyio
@pytest.mark.parametrize("operation_id", ["", "x" * 129])
async def test_poll_rejects_invalid_operation_id_without_draining(
    operation_id: str,
) -> None:
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []

    @server.widget
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        instance_id = launch.meta["anywidget"]["instanceId"]
        created[0].doubled = 22
        invalid = await client.call_tool(
            "anywidget_poll",
            {"instance_id": instance_id, "operation_id": operation_id},
        )
        valid = await client.call_tool(
            "anywidget_poll",
            {"instance_id": instance_id, "operation_id": "valid-poll"},
        )

    assert invalid.isError is True
    assert isinstance(invalid.content[0], TextContent)
    assert (
        "operation_id must contain between 1 and 128 characters"
        in invalid.content[0].text
    )
    assert valid.meta is not None
    assert any(
        message["data"]["state"] == {"doubled": 22}
        for message in valid.meta["anywidget"]["messages"]
    )


@pytest.mark.anyio
async def test_poll_returns_python_originated_update() -> None:
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []

    @server.widget
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        created[0].doubled = 22
        result = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": launch.meta["anywidget"]["instanceId"],
                "operation_id": "python-originated-update",
            },
        )

    assert result.meta is not None
    messages = result.meta["anywidget"]["messages"]
    assert any(
        message["data"]["method"] == "update"
        and message["data"]["state"] == {"doubled": 22}
        for message in messages
    )
    assert result.meta["anywidget"]["context"] == {
        "version": 2,
        "tool": "counter",
        "state": {"doubled": 22, "value": 0},
    }


@pytest.mark.anyio
async def test_poll_returns_dynamic_widget_models_in_app_metadata() -> None:
    server = AnyWidgetMCP("test")
    created: list[tuple[ParentWidget, CounterWidget]] = []

    @server.widget(state=None)
    def parent() -> ParentWidget:
        child = CounterWidget(value=1)
        root = ParentWidget(child=child)
        created.append((root, child))
        return root

    async with connected(server) as client:
        launch = await client.call_tool("parent", {})
        assert launch.meta is not None
        root, first = created[0]
        first_model_id = first.model_id
        second = CounterWidget(value=2)
        root.child = second
        second_model_id = second.model_id
        result = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": launch.meta["anywidget"]["instanceId"],
                "operation_id": "dynamic-widget-models",
            },
        )
        before_ack = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": launch.meta["anywidget"]["instanceId"],
                "model_id": first_model_id,
                "operation_id": "detached-before-ack",
                "data": {"method": "request_state"},
            },
        )
        assert before_ack.isError is False
        assert first.comm is not None
        acknowledged = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": launch.meta["anywidget"]["instanceId"],
                "operation_id": "acknowledge-dynamic-removal",
                "acknowledged_model_ids": [first_model_id],
            },
        )
        assert acknowledged.isError is False
        assert first.comm is None
        after_ack = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": launch.meta["anywidget"]["instanceId"],
                "model_id": first_model_id,
                "operation_id": "detached-after-ack",
                "data": {"method": "request_state"},
            },
        )

    assert result.structuredContent is None
    assert result.meta is not None
    payload = result.meta["anywidget"]
    assert payload["models"][second_model_id]["state"]["value"] == 2
    assert payload["removedModelIds"] == [first_model_id]
    assert after_ack.isError is True
    assert isinstance(after_ack.content[0], TextContent)
    assert "Unknown widget model" in after_ack.content[0].text


@pytest.mark.anyio
async def test_launch_result_keeps_an_immutable_model_snapshot() -> None:
    server = AnyWidgetMCP("test")
    created: list[tuple[ParentWidget, CounterWidget]] = []

    @server.widget(state=None)
    def parent() -> ParentWidget:
        first = CounterWidget(value=1)
        root = ParentWidget(child=first)
        created.append((root, first))
        return root

    async with connected(server) as client:
        result = await client.call_tool("parent", {})
        assert result.meta is not None
        payload = result.meta["anywidget"]
        root, first = created[0]
        root_model_id = root.model_id
        first_model_id = first.model_id
        root.child = CounterWidget(value=2)

    assert set(payload["models"]) == {root_model_id, first_model_id}
    assert (
        payload["models"][root_model_id]["state"]["child"]
        == f"anywidget:{first_model_id}"
    )


@pytest.mark.anyio
async def test_launch_rejects_projection_that_replaces_child() -> None:
    server = AnyWidgetMCP("test")
    created: list[tuple[ParentWidget, CounterWidget, CounterWidget]] = []

    def project(widget: AnyWidget) -> dict[str, int]:
        assert isinstance(widget, ParentWidget)
        _root, first, second = created[0]
        if widget.child is first:
            widget.child = second
        return {"value": widget.child.value}

    @server.widget(state=project)
    def parent() -> ParentWidget:
        first = CounterWidget(value=1)
        second = CounterWidget(value=2)
        root = ParentWidget(child=first)
        created.append((root, first, second))
        return root

    async with connected(server) as client:
        launch = await client.call_tool("parent", {})

    assert launch.isError is True
    assert isinstance(launch.content[0], TextContent)
    assert "State projection callables must not mutate" in launch.content[0].text
    root, first, second = created[0]
    assert root.comm is None
    assert first.comm is None
    assert second.comm is None


@pytest.mark.anyio
async def test_launch_includes_messages_that_advance_serialized_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = WidgetSession.launch_snapshot

    def mutate_before_launch_snapshot(session: WidgetSession) -> SessionSnapshot:
        assert isinstance(session.root, CounterWidget)
        session.root.value = 8
        return original(session)

    monkeypatch.setattr(
        WidgetSession,
        "launch_snapshot",
        mutate_before_launch_snapshot,
    )
    server = AnyWidgetMCP("test")

    @server.widget
    def counter() -> CounterWidget:
        return CounterWidget()

    async with connected(server) as client:
        result = await client.call_tool("counter", {})

    assert result.meta is not None
    runtime = result.meta["anywidget"]
    model_id = runtime["rootModelId"]
    assert runtime["models"][model_id]["state"]["value"] == 0
    assert [message["data"]["state"] for message in runtime["messages"]] == [
        {"value": 8},
        {"doubled": 16},
    ]
    assert runtime["context"]["state"]["value"] == 8


@pytest.mark.anyio
async def test_launch_snapshot_failure_closes_unregistered_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_launch_snapshot(_session: WidgetSession) -> SessionSnapshot:
        raise TimeoutError("snapshot stalled")

    monkeypatch.setattr(WidgetSession, "launch_snapshot", fail_launch_snapshot)
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []

    @server.widget(state=None)
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    async with connected(server) as client:
        result = await client.call_tool("counter", {})

    assert result.isError is True
    assert isinstance(result.content[0], TextContent)
    assert "Widget launch snapshot failed" in result.content[0].text
    assert created[0].comm is None


@pytest.mark.anyio
async def test_idle_poll_does_not_repeat_model_context() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget)

    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {})
        assert launch.meta is not None
        result = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": launch.meta["anywidget"]["instanceId"],
                "operation_id": "idle-model-context",
            },
        )

    assert result.meta is not None
    assert "context" not in result.meta["anywidget"]


@pytest.mark.anyio
async def test_state_none_suppresses_live_context() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget, state=None)

    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]
        result = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": payload["instanceId"],
                "model_id": payload["rootModelId"],
                "operation_id": "state-none-update",
                "data": {
                    "method": "update",
                    "state": {"value": 8},
                    "buffer_paths": [],
                },
            },
        )

    assert result.isError is False
    assert result.meta is not None
    assert "context" not in result.meta["anywidget"]
