from __future__ import annotations

from typing import Any

import pytest
from mcp.types import TextContent

import anywidget_mcp._runtime as runtime_module
from anywidget_mcp.server import AnyWidgetMCP

from ._server_support import (
    CounterWidget,
    bootstrap_runtime,
    connected,
    state_id,
)


@pytest.mark.anyio
async def test_state_tool_reads_browser_updates_after_comm_delivery() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget)

    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {"value": 2})
        runtime = await bootstrap_runtime(client, launch)
        comm = await client.comm(
            {
                "instance_id": runtime["instanceId"],
                "model_id": runtime["rootModelId"],
                "operation_id": 1,
                "data": {
                    "method": "update",
                    "state": {"value": 7},
                    "buffer_paths": [],
                },
            },
        )
        current = await client.call_tool(
            "anywidget_state",
            {"state_id": state_id(launch)},
        )

    assert comm.meta is not None
    assert comm.meta["anywidget"]["context"] == {
        "version": 2,
        "tool": "counter_widget",
        "state": {"doubled": 14, "value": 7},
    }
    assert current.structured_content == {
        "state_id": state_id(launch),
        "tool": "counter_widget",
        "version": 2,
        "state": {"doubled": 14, "value": 7},
    }
    assert current.content == [
        TextContent(
            type="text",
            text='Current counter_widget state: {"doubled":14,"value":7}.',
        )
    ]


@pytest.mark.anyio
async def test_state_read_does_not_consume_the_next_app_poll() -> None:
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []

    @server.widget
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        runtime = await bootstrap_runtime(client, launch)
        created[0].value = 4

        current = await client.call_tool(
            "anywidget_state",
            {"state_id": state_id(launch)},
        )
        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": runtime["instanceId"],
                "operation_id": 1,
            },
        )

    assert current.structured_content is not None
    assert current.structured_content["state"] == {"doubled": 8, "value": 4}
    assert poll.meta is not None
    assert poll.meta["anywidget"]["context"] == {
        "version": 2,
        "tool": "counter",
        "state": {"doubled": 8, "value": 4},
    }


@pytest.mark.anyio
async def test_state_handles_keep_live_widget_instances_isolated() -> None:
    server = AnyWidgetMCP("test")

    @server.widget
    def counter(value: int) -> CounterWidget:
        return CounterWidget(value=value)

    async with connected(server) as client:
        first = await client.call_tool("counter", {"value": 1})
        second = await client.call_tool("counter", {"value": 9})
        await bootstrap_runtime(client, first, operation_id="bootstrap-first")
        await bootstrap_runtime(client, second, operation_id="bootstrap-second")

        first_state = await client.call_tool(
            "anywidget_state",
            {"state_id": state_id(first)},
        )
        second_state = await client.call_tool(
            "anywidget_state",
            {"state_id": state_id(second)},
        )

    assert state_id(first) != state_id(second)
    assert first_state.structured_content is not None
    assert second_state.structured_content is not None
    assert first_state.structured_content["state"] == {"doubled": 2, "value": 1}
    assert second_state.structured_content["state"] == {"doubled": 18, "value": 9}


@pytest.mark.anyio
async def test_state_none_omits_a_read_handle() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget, state=None)

    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {})
        unavailable = await client.call_tool(
            "anywidget_state",
            {"state_id": "0" * 32},
        )

    assert launch.structured_content == {"tool": "counter_widget"}
    assert unavailable.is_error is True
    assert isinstance(unavailable.content[0], TextContent)
    assert unavailable.content[0].text.endswith("Widget state is unavailable")


@pytest.mark.anyio
async def test_disposal_revokes_the_state_handle() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget)

    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {})
        runtime = await bootstrap_runtime(client, launch)
        await client.call_tool(
            "anywidget_dispose",
            {"session_id": runtime["instanceId"]},
        )
        unavailable = await client.call_tool(
            "anywidget_state",
            {"state_id": state_id(launch)},
        )

    assert unavailable.is_error is True
    assert isinstance(unavailable.content[0], TextContent)
    assert unavailable.content[0].text.endswith("Widget state is unavailable")
    assert state_id(launch) not in unavailable.content[0].text


@pytest.mark.anyio
async def test_prebootstrap_state_read_renews_the_configured_idle_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    clock = [0.0]
    monkeypatch.setattr(
        runtime_module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])
    )
    server = AnyWidgetMCP("state-activity", session_idle_timeout=60)
    server.widget(CounterWidget)
    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {})
        active = server._widget_tools._require_runtime()
        lease = active._state_handles[state_id(launch)]
        clock[0] = 31.0
        current = await client.call_tool(
            "anywidget_state", {"state_id": state_id(launch)}
        )
        assert not current.is_error
        assert lease.deadline == 91


@pytest.mark.anyio
async def test_state_projection_error_remains_readable_after_comm_delivery() -> None:
    server = AnyWidgetMCP("test")

    def project(widget: CounterWidget) -> dict[str, Any]:
        if widget.value == 1:
            raise ValueError("one is temporarily unavailable")
        return {"value": widget.value}

    server.widget(CounterWidget, state=project)

    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {"value": 0})
        runtime = await bootstrap_runtime(client, launch)
        failed_comm = await client.comm(
            {
                "instance_id": runtime["instanceId"],
                "model_id": runtime["rootModelId"],
                "operation_id": 1,
                "data": {
                    "method": "update",
                    "state": {"value": 1},
                    "buffer_paths": [],
                },
            },
        )
        failed_read = await client.call_tool(
            "anywidget_state",
            {"state_id": state_id(launch)},
        )
        await client.comm(
            {
                "instance_id": runtime["instanceId"],
                "model_id": runtime["rootModelId"],
                "operation_id": 2,
                "data": {
                    "method": "update",
                    "state": {"value": 2},
                    "buffer_paths": [],
                },
            },
        )
        recovered = await client.call_tool(
            "anywidget_state",
            {"state_id": state_id(launch)},
        )

    assert failed_comm.meta is not None
    assert failed_comm.meta["anywidget"]["contextError"] == (
        "Widget state projection failed: one is temporarily unavailable"
    )
    assert failed_read.is_error is True
    assert isinstance(failed_read.content[0], TextContent)
    assert failed_read.content[0].text.endswith(
        "Widget state projection failed: one is temporarily unavailable"
    )
    assert recovered.structured_content is not None
    assert recovered.structured_content["state"] == {"value": 2}
