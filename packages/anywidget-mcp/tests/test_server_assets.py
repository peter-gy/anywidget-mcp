from __future__ import annotations

import pytest
from mcp.types import TextContent
from wigglystuff import ColorPicker

from anywidget_mcp.server import AnyWidgetMCP

from ._server_support import CounterWidget, bootstrap_runtime, connected, read_blob


@pytest.mark.anyio
async def test_color_picker_launch_returns_widget_model_and_sources() -> None:
    server = AnyWidgetMCP("test")
    server.widget(ColorPicker)
    async with connected(server) as client:
        result = await client.call_tool(
            "color_picker", {"color": "#c026d3", "show_label": False}
        )
        payload = await bootstrap_runtime(client, result)
        model = payload["models"][payload["rootModelId"]]
        sources = {
            name: await read_blob(client, payload["instanceId"], ref)
            for name, ref in model["sourceRefs"].items()
        }
    assert result.meta is not None
    assert result.meta["ui"] == {"resourceUri": "ui://anywidget-mcp/app.html"}
    assert model["state"]["color"] == "#c026d3"
    assert model["state"]["show_label"] is False
    assert set(sources) == {"_esm", "_css"}
    assert all(sources.values())


@pytest.mark.anyio
async def test_attachments_are_accessible_within_the_owning_live_session() -> None:
    class First(CounterWidget):
        _esm = "export default {render(){return 'first'}}"

    class Second(CounterWidget):
        _esm = "export default {render(){return 'second'}}"

    server = AnyWidgetMCP("test")
    server.widget(First)
    server.widget(Second)
    async with connected(server) as client:
        first = await bootstrap_runtime(client, await client.call_tool("first", {}))
        second = await bootstrap_runtime(client, await client.call_tool("second", {}))
        ref = first["models"][first["rootModelId"]]["sourceRefs"]["_esm"]
        assert await read_blob(client, first["instanceId"], ref) == First._esm.encode()
        foreign = await client.call_tool(
            "anywidget_read",
            {"instance_id": second["instanceId"], "blob_id": ref["id"]},
        )
        await client.call_tool("anywidget_dispose", {"session_id": first["instanceId"]})
        disposed = await client.call_tool(
            "anywidget_read", {"instance_id": first["instanceId"], "blob_id": ref["id"]}
        )
    assert foreign.is_error
    assert isinstance(foreign.content[0], TextContent)
    assert "Unknown widget attachment" in foreign.content[0].text
    assert disposed.is_error
    assert isinstance(disposed.content[0], TextContent)
    assert "Unknown widget session" in disposed.content[0].text


@pytest.mark.anyio
async def test_attachment_reads_preserve_bootstrap_until_a_protocol_response() -> None:
    widget = CounterWidget()
    server = AnyWidgetMCP("test")
    server.widget(lambda: widget, name="counter")
    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        runtime = await bootstrap_runtime(client, launch)
        ref = runtime["models"][runtime["rootModelId"]]["sourceRefs"]["_esm"]
        widget._esm = "export default {render(){return 'updated'}}"
        assert await read_blob(client, runtime["instanceId"], ref)
        assert await bootstrap_runtime(client, launch) == runtime
        await client.call_tool(
            "anywidget_poll",
            {"instance_id": runtime["instanceId"], "operation_id": 1},
        )
        stale = await client.call_tool(
            "anywidget_read",
            {"instance_id": runtime["instanceId"], "blob_id": ref["id"]},
        )
    assert stale.is_error


@pytest.mark.anyio
async def test_source_replay_retention_follows_acknowledgments() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget)
    async with connected(server) as client:
        runtime = await bootstrap_runtime(
            client, await client.call_tool("counter_widget", {})
        )

        async def update(source: str, operation: int):
            return await client.comm(
                {
                    "instance_id": runtime["instanceId"],
                    "model_id": runtime["rootModelId"],
                    "operation_id": operation,
                    "data": {"method": "update", "state": {"_esm": source}},
                },
            )

        first = await update("first", 1)
        await update("second", 2)
        assert first.meta is not None
        first_payload = first.meta["anywidget"]
        ref = first_payload["messages"][0]["sourceRefs"]["_esm"]
        replay = await update("first", 1)
        assert replay.meta == first.meta
        assert await read_blob(client, runtime["instanceId"], ref) == b"first"
        await update("third", 3)
        await update("fourth", 4)
        await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": runtime["instanceId"],
                "operation_id": 5,
                "acknowledged_operation_id": 4,
            },
        )
        released = await client.call_tool(
            "anywidget_read",
            {"instance_id": runtime["instanceId"], "blob_id": ref["id"]},
        )
        retired = await update("first", 1)
    assert released.is_error
    assert retired.is_error
