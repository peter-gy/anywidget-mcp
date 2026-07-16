from __future__ import annotations

import anywidget_mcp.server as server_module
import pytest
from mcp.types import TextContent
from wigglystuff import ColorPicker

from anywidget_mcp import AnyWidgetMCP

from ._server_support import CounterWidget, connected


@pytest.mark.anyio
async def test_color_picker_launch_returns_widget_runtime_payload() -> None:
    server = AnyWidgetMCP("test")

    @server.widget
    def color_picker(color: str = "#315efb", show_label: bool = True) -> ColorPicker:
        return ColorPicker(color=color, show_label=show_label)

    async with connected(server) as client:
        result = await client.call_tool(
            "color_picker", {"color": "#c026d3", "show_label": False}
        )

    assert result.isError is False
    assert result.structuredContent == {
        "tool": "color_picker",
        "state": {"color": "#c026d3", "show_label": False},
    }
    assert result.content == [
        TextContent(
            type="text",
            text=(
                'Opened Color Picker with state {"color":"#c026d3","show_label":false}.'
            ),
        )
    ]
    assert result.meta is not None
    assert result.meta["ui"] == {"resourceUri": "ui://anywidget-mcp/widget.html"}
    payload = result.meta["anywidget"]
    assert payload["rootModelId"] in payload["models"]
    model = payload["models"][payload["rootModelId"]]
    assert model["state"]["color"] == "#c026d3"
    assert model["state"]["show_label"] is False
    assert payload["protocolVersion"] == 1
    assert set(model["sourceRefs"]) == {"_esm", "_css"}
    assert set(model["sourceRefs"].values()) == set(payload["assetManifest"])
    assert payload["context"] == {
        "version": 1,
        "tool": "color_picker",
        "state": {"color": "#c026d3", "show_label": False},
    }


@pytest.mark.anyio
async def test_assets_are_fetched_by_digest_within_the_owning_session() -> None:
    class FirstAssetWidget(CounterWidget):
        _esm = "export default { render() { return 'first'; } }"
        _css = ".first { color: red; }"

    class SecondAssetWidget(CounterWidget):
        _esm = "export default { render() { return 'second'; } }"
        _css = ".second { color: blue; }"

    server = AnyWidgetMCP("test")
    server.widget(FirstAssetWidget)
    server.widget(SecondAssetWidget)

    async with connected(server) as client:
        first = await client.call_tool("first_asset_widget", {})
        second = await client.call_tool("second_asset_widget", {})
        assert first.meta is not None
        assert second.meta is not None
        first_runtime = first.meta["anywidget"]
        second_runtime = second.meta["anywidget"]
        asset_ids = list(first_runtime["assetManifest"])

        fetched = await client.call_tool(
            "anywidget_assets",
            {
                "instance_id": first_runtime["instanceId"],
                "asset_ids": asset_ids,
            },
        )
        foreign = await client.call_tool(
            "anywidget_assets",
            {
                "instance_id": second_runtime["instanceId"],
                "asset_ids": asset_ids,
            },
        )
        await client.call_tool(
            "anywidget_dispose",
            {"instance_id": first_runtime["instanceId"]},
        )
        disposed = await client.call_tool(
            "anywidget_assets",
            {
                "instance_id": first_runtime["instanceId"],
                "asset_ids": asset_ids,
            },
        )

    assert fetched.isError is False
    assert fetched.meta is not None
    contents = fetched.meta["anywidget"]["assetContents"]
    assert set(contents) == set(asset_ids)
    assert {asset["kind"] for asset in contents.values()} == {"esm", "css"}
    assert {asset["text"] for asset in contents.values()} == {
        "export default { render() { return 'first'; } }",
        ".first { color: red; }",
    }
    assert foreign.isError is True
    assert isinstance(foreign.content[0], TextContent)
    assert "Unknown widget asset" in foreign.content[0].text
    assert disposed.isError is True
    assert isinstance(disposed.content[0], TextContent)
    assert "Unknown widget session" in disposed.content[0].text


@pytest.mark.anyio
async def test_asset_sources_follow_the_bounded_comm_replay_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "_COMM_REPLAY_LIMIT", 2)
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget)

    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {})
        assert launch.meta is not None
        runtime = launch.meta["anywidget"]
        instance_id = runtime["instanceId"]
        model_id = runtime["rootModelId"]

        async def update_source(source: str, operation_id: str):
            return await client.call_tool(
                "anywidget_comm",
                {
                    "instance_id": instance_id,
                    "model_id": model_id,
                    "data": {
                        "method": "update",
                        "state": {"_esm": source},
                        "buffer_paths": [],
                    },
                    "operation_id": operation_id,
                },
            )

        first = await update_source(
            "export default { render() { return 'first'; } }", "first"
        )
        second = await update_source(
            "export default { render() { return 'second'; } }", "second"
        )
        assert first.meta is not None
        assert second.isError is False
        first_runtime = first.meta["anywidget"]
        first_id = next(iter(first_runtime["assetManifest"]))

        replayed = await update_source(
            "export default { render() { return 'first'; } }", "first"
        )
        fetched = await client.call_tool(
            "anywidget_assets",
            {"instance_id": instance_id, "asset_ids": [first_id]},
        )

        await update_source("export default { render() { return 'third'; } }", "third")
        await update_source(
            "export default { render() { return 'fourth'; } }", "fourth"
        )
        evicted = await client.call_tool(
            "anywidget_assets",
            {"instance_id": instance_id, "asset_ids": [first_id]},
        )

    assert replayed.meta is not None
    assert replayed.meta["anywidget"]["assetManifest"] == first_runtime["assetManifest"]
    assert fetched.isError is False
    assert fetched.meta is not None
    assert fetched.meta["anywidget"]["assetContents"][first_id]["text"] == (
        "export default { render() { return 'first'; } }"
    )
    assert evicted.isError is True
    assert isinstance(evicted.content[0], TextContent)
    assert "Unknown widget asset" in evicted.content[0].text
