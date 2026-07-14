from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import threading
import time
from typing import Any, cast

import anyio
import pytest
from anywidget import AnyWidget, WidgetTrait
from mcp.client.session import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent
from pydantic import AnyUrl
from starlette.testclient import TestClient
from traitlets import Int, observe, validate
from wigglystuff import ColorPicker, Matrix, Slider2D, SortableList

from anywidget_mcp import (
    APP_MIME_TYPE,
    APP_RESOURCE_URI,
    AnyWidgetMCP,
    WidgetTools,
    attach,
)
from anywidget_mcp._bridge import SessionSnapshot, WidgetSession
from anywidget_mcp._state import DEFAULT_STATE
from anywidget_mcp.server import describe_widget_target


class CounterWidget(AnyWidget):
    _esm = "export default { render() {} }"

    value = Int(0).tag(sync=True)
    doubled = Int(0).tag(sync=True)

    @observe("value")
    def _update_doubled(self, change: dict[str, Any]) -> None:
        self.doubled = change["new"] * 2


class ParentWidget(AnyWidget):
    _esm = "export default { render() {} }"

    child = WidgetTrait().tag(sync=True)


class ValidatedCounterWidget(AnyWidget):
    _esm = "export default { render() {} }"

    value = Int(0).tag(sync=True)

    @validate("value")
    def _clamp_value(self, proposal: dict[str, Any]) -> int:
        return min(10, max(0, proposal["value"]))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def connected(server: AnyWidgetMCP) -> AsyncGenerator[ClientSession, None]:
    try:
        async with create_connected_server_and_client_session(
            server, raise_exceptions=True
        ) as client:
            yield client
    finally:
        server.close()


@pytest.mark.anyio
async def test_attach_registers_classes_sync_factories_and_async_factories() -> None:
    mcp = FastMCP("test")
    widgets = attach(mcp)
    created: list[CounterWidget] = []

    assert isinstance(widgets, WidgetTools)
    assert widgets.widget(ColorPicker, state="color") is ColorPicker

    @widgets.widget
    def counter(value: int = 1) -> CounterWidget:
        widget = CounterWidget(value=value)
        created.append(widget)
        return widget

    @widgets.widget
    async def async_counter(value: int = 2) -> CounterWidget:
        widget = CounterWidget(value=value)
        created.append(widget)
        return widget

    try:
        async with create_connected_server_and_client_session(
            mcp,
            raise_exceptions=True,
        ) as client:
            color = await client.call_tool("color_picker", {"color": "#c026d3"})
            sync_result = await client.call_tool("counter", {"value": 3})
            async_result = await client.call_tool("async_counter", {"value": 4})
    finally:
        widgets.close()

    assert color.structuredContent == {
        "tool": "color_picker",
        "state": {"color": "#c026d3"},
    }
    assert color.content == [
        TextContent(
            type="text",
            text='Opened Color Picker with state {"color":"#c026d3"}.',
        )
    ]
    assert color.meta is not None
    assert color.meta["anywidget"]["context"] == {
        "version": 1,
        "tool": "color_picker",
        "state": {"color": "#c026d3"},
    }
    assert sync_result.structuredContent == {
        "tool": "counter",
        "state": {"doubled": 6, "value": 3},
    }
    assert async_result.structuredContent == {
        "tool": "async_counter",
        "state": {"doubled": 8, "value": 4},
    }
    assert all(widget.comm is None for widget in created)


def test_attach_rejects_a_second_adapter() -> None:
    mcp = FastMCP("test")
    widgets = attach(mcp)
    try:
        with pytest.raises(ValueError, match="already attached"):
            attach(mcp)
    finally:
        widgets.close()


def test_describe_widget_target_matches_registered_input_schema() -> None:
    description = describe_widget_target(
        ColorPicker,
        name="choose_color",
        title="Choose Color",
    )
    mcp = FastMCP("test")
    widgets = attach(mcp)
    try:
        widgets.widget(
            ColorPicker,
            name="choose_color",
            title="Choose Color",
        )
        tool = mcp._tool_manager.get_tool("choose_color")
    finally:
        widgets.close()

    assert description.target_name == "ColorPicker"
    assert description.tool_name == "choose_color"
    assert description.title == "Choose Color"
    assert description.kind == "widget-class"
    assert tool is not None
    assert description.input_schema == tool.parameters


@pytest.mark.parametrize("failure", [False, True], ids=["return", "raise"])
def test_owned_runtime_closes_widget_sessions(
    failure: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = AnyWidgetMCP("test")
    widget = CounterWidget()
    server._widget_tools._open_widget(
        widget,
        None,
        tool_name="counter",
        tool_title="Counter",
    )

    def run_runtime(
        _server: FastMCP,
        transport: str = "stdio",
        mount_path: str | None = None,
    ) -> None:
        del transport, mount_path
        if failure:
            raise RuntimeError("runtime failed")

    monkeypatch.setattr(FastMCP, "run", run_runtime)

    if failure:
        with pytest.raises(RuntimeError, match="runtime failed"):
            server.run()
    else:
        server.run()

    assert widget.comm is None
    assert server._widget_tools._sessions == {}


@pytest.mark.anyio
async def test_widget_decorator_exposes_factory_schema_and_app_metadata() -> None:
    server = AnyWidgetMCP("test")

    @server.widget(name="open_counter", title="Counter")
    def counter(label: str, value: int = 3, enabled: bool = True) -> CounterWidget:
        del label, enabled
        return CounterWidget(value=value)

    async with connected(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    tool = tools["open_counter"]
    properties = tool.inputSchema["properties"]
    assert tool.title == "Counter"
    assert tool.inputSchema["required"] == ["label"]
    assert properties["label"]["type"] == "string"
    assert properties["value"] == {
        "default": 3,
        "title": "Value",
        "type": "integer",
    }
    assert properties["enabled"] == {
        "default": True,
        "title": "Enabled",
        "type": "boolean",
    }
    assert tool.meta == {
        "ui": {"resourceUri": APP_RESOURCE_URI},
    }


@pytest.mark.anyio
async def test_widget_classes_expose_filtered_constructor_schemas() -> None:
    server = AnyWidgetMCP("test")
    assert server.widget(ColorPicker) is ColorPicker
    server.widget(SortableList)
    server.widget(Matrix)
    server.widget(Slider2D)

    async with connected(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        launches = {
            "color_picker": await client.call_tool("color_picker", {}),
            "sortable_list": await client.call_tool(
                "sortable_list", {"value": ["alpha", "beta"]}
            ),
            "matrix": await client.call_tool("matrix", {}),
            "slider_2d": await client.call_tool("slider_2d", {}),
        }

    assert {
        name
        for name, tool in tools.items()
        if tool.meta == {"ui": {"resourceUri": APP_RESOURCE_URI}}
    } == {"color_picker", "sortable_list", "matrix", "slider_2d"}
    assert "kwargs" not in tools["color_picker"].inputSchema["properties"]
    assert tools["sortable_list"].inputSchema["required"] == ["value"]
    assert tools["sortable_list"].inputSchema["properties"]["value"] == {
        "items": {"type": "string"},
        "title": "Value",
        "type": "array",
    }
    assert tools["slider_2d"].inputSchema["properties"]["x_bounds"]["maxItems"] == 2
    assert tools["slider_2d"].inputSchema["properties"]["x_bounds"]["minItems"] == 2
    assert tools["color_picker"].description is not None
    assert tools["color_picker"].description.startswith("Simple color picker")
    assert tools["color_picker"].title == "Color Picker"
    assert tools["slider_2d"].title == "Slider 2D"
    assert all(result.isError is False for result in launches.values())


def test_widget_class_overrides_inferred_tool_fields() -> None:
    server = AnyWidgetMCP("test")
    server.widget(
        ColorPicker,
        name="choose_color",
        title="Choose color",
        description="Select a hexadecimal color.",
    )

    tool = server._tool_manager.get_tool("choose_color")
    server.close()

    assert tool is not None
    assert tool.title == "Choose color"
    assert tool.description == "Select a hexadecimal color."


def test_widget_class_description_uses_its_own_docstring() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget)

    tool = server._tool_manager.get_tool("counter_widget")
    server.close()

    assert tool is not None
    assert tool.description == ""


@pytest.mark.parametrize(
    "name", ["anywidget_comm", "anywidget_poll", "anywidget_dispose"]
)
def test_widget_rejects_reserved_session_tool_names(name: str) -> None:
    server = AnyWidgetMCP("test")

    with pytest.raises(ValueError, match="reserved for the AnyWidget app"):
        server.widget(CounterWidget, name=name)

    server.close()


def test_widget_rejects_duplicate_tool_names_and_preserves_the_first() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget, name="counter", title="First counter")

    with pytest.raises(ValueError, match="already registered"):
        server.widget(CounterWidget, name="counter", title="Second counter")

    tool = server._tool_manager.get_tool("counter")
    server.close()
    assert tool is not None
    assert tool.title == "First counter"


def test_widget_rejects_instances_and_unrelated_classes() -> None:
    server = AnyWidgetMCP("test")
    picker = ColorPicker()

    try:
        with pytest.raises(TypeError, match="Pass the ColorPicker class.*fresh widget"):
            server.widget(cast(Any, picker))
        with pytest.raises(TypeError, match="expected an AnyWidget subclass"):
            server.widget(str)  # type: ignore[arg-type]
    finally:
        picker.close()
        server.close()


@pytest.mark.anyio
async def test_async_factory_returns_a_fresh_widget_per_invocation() -> None:
    server = AnyWidgetMCP("test")
    created: list[CounterWidget] = []

    @server.widget
    async def counter(value: int = 1) -> CounterWidget:
        widget = CounterWidget(value=value)
        created.append(widget)
        return widget

    async with connected(server) as client:
        first = await client.call_tool("counter", {"value": 2})
        second = await client.call_tool("counter", {"value": 3})

    assert first.isError is False
    assert second.isError is False
    assert created[0] is not created[1]


@pytest.mark.anyio
async def test_session_tools_are_visible_to_the_app() -> None:
    server = AnyWidgetMCP("test")

    async with connected(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    for name in ("anywidget_comm", "anywidget_poll", "anywidget_dispose"):
        assert tools[name].meta == {"ui": {"visibility": ["app"]}}
    assert "operation_id" in tools["anywidget_comm"].inputSchema["required"]
    assert "operation_id" in tools["anywidget_poll"].inputSchema["required"]


@pytest.mark.anyio
async def test_app_resource_exposes_mime_type_and_csp() -> None:
    server = AnyWidgetMCP(
        "test",
        csp={
            "connectDomains": ["https://api.example.com"],
            "resourceDomains": ["https://esm.sh"],
        },
    )

    async with connected(server) as client:
        resources = (await client.list_resources()).resources
        contents = (await client.read_resource(AnyUrl(APP_RESOURCE_URI))).contents

    assert len(resources) == 1
    assert resources[0].mimeType == APP_MIME_TYPE
    assert resources[0].meta == {
        "ui": {
            "csp": {
                "connectDomains": ["https://api.example.com"],
                "resourceDomains": ["https://esm.sh", "blob:"],
            },
            "prefersBorder": True,
        }
    }
    assert len(contents) == 1
    assert contents[0].mimeType == APP_MIME_TYPE
    assert contents[0].meta == resources[0].meta


def test_streamable_http_app_allows_configured_browser_origin() -> None:
    server = AnyWidgetMCP("test", cors_origins=["http://localhost:8080"])

    with TestClient(server.streamable_http_app()) as client:
        response = client.options(
            "/mcp",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "mcp-protocol-version",
            },
        )
        head_preflight = client.options(
            "/mcp",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "HEAD",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        simple_response = client.get(
            "/missing", headers={"Origin": "http://localhost:8080"}
        )
        head_response = client.head("/mcp", headers={"Origin": "http://localhost:8080"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:8080"
    assert "mcp-protocol-version" in response.headers["access-control-allow-headers"]
    assert head_preflight.status_code == 200
    assert "HEAD" in head_preflight.headers["access-control-allow-methods"]
    assert simple_response.headers["access-control-expose-headers"] == "Mcp-Session-Id"
    assert head_response.status_code == 200
    assert head_response.headers["allow"] == "GET, POST, DELETE, HEAD"
    assert (
        head_response.headers["access-control-allow-origin"] == "http://localhost:8080"
    )
    server.close()


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
    assert result.meta["ui"] == {"resourceUri": APP_RESOURCE_URI}
    payload = result.meta["anywidget"]
    assert payload["rootModelId"] in payload["models"]
    model = payload["models"][payload["rootModelId"]]
    assert model["state"]["color"] == "#c026d3"
    assert model["state"]["show_label"] is False
    assert model["state"]["_esm"]
    assert model["state"]["_css"]
    assert payload["context"] == {
        "version": 1,
        "tool": "color_picker",
        "state": {"color": "#c026d3", "show_label": False},
    }


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
    assert idle.meta["anywidget"] == {"messages": []}


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
        next_poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": instance_id,
                "operation_id": "poll-operation-2",
            },
        )

    assert replay.meta == first.meta
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

    assert result.structuredContent is None
    assert result.meta is not None
    payload = result.meta["anywidget"]
    assert payload["models"][second_model_id]["state"]["value"] == 2
    assert payload["removedModelIds"] == [first_model_id]


def test_launch_result_keeps_an_immutable_model_snapshot() -> None:
    server = AnyWidgetMCP("test")
    first = CounterWidget(value=1)
    root = ParentWidget(child=first)
    result = server._widget_tools._open_widget(
        root,
        None,
        tool_name="parent",
        tool_title="Parent",
    )
    assert result.meta is not None
    payload = result.meta["anywidget"]
    first_model_id = first.model_id

    root.child = CounterWidget(value=2)

    assert set(payload["models"]) == {root.model_id, first_model_id}
    assert (
        payload["models"][root.model_id]["state"]["child"]
        == f"anywidget:{first_model_id}"
    )
    server.close()


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


def test_launch_includes_messages_that_advance_serialized_models(
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
    widget = CounterWidget()

    try:
        result = server._widget_tools._open_widget(
            widget,
            DEFAULT_STATE,
            tool_name="counter",
            tool_title="Counter",
        )
        model_id = widget.model_id
    finally:
        server.close()

    assert result.meta is not None
    runtime = result.meta["anywidget"]
    assert runtime["models"][model_id]["state"]["value"] == 0
    assert [message["data"]["state"] for message in runtime["messages"]] == [
        {"value": 8},
        {"doubled": 16},
    ]
    assert runtime["context"]["state"]["value"] == 8


def test_launch_snapshot_failure_closes_unregistered_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_launch_snapshot(_session: WidgetSession) -> SessionSnapshot:
        raise TimeoutError("snapshot stalled")

    monkeypatch.setattr(WidgetSession, "launch_snapshot", fail_launch_snapshot)
    server = AnyWidgetMCP("test")
    widget = CounterWidget()

    try:
        with pytest.raises(ToolError, match="Widget launch snapshot failed"):
            server._widget_tools._open_widget(
                widget,
                None,
                tool_name="counter",
                tool_title="Counter",
            )

        assert widget.comm is None
        assert server._widget_tools._sessions == {}
    finally:
        server.close()


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


@pytest.mark.anyio
async def test_dispose_releases_widget_session() -> None:
    server = AnyWidgetMCP("test")

    @server.widget
    def counter() -> CounterWidget:
        return CounterWidget()

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        instance_id = launch.meta["anywidget"]["instanceId"]
        first = await client.call_tool(
            "anywidget_dispose", {"instance_id": instance_id}
        )
        second = await client.call_tool(
            "anywidget_dispose", {"instance_id": instance_id}
        )
        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": instance_id,
                "operation_id": "poll-after-dispose",
            },
        )

    assert first.structuredContent == {"disposed": True}
    assert second.structuredContent == {"disposed": False}
    assert poll.isError is True
    assert isinstance(poll.content[0], TextContent)
    assert "Unknown widget session" in poll.content[0].text


@pytest.mark.anyio
async def test_idle_session_expires_when_an_app_never_claims_it() -> None:
    closed = threading.Event()

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            super().close()
            closed.set()

    server = AnyWidgetMCP("test", session_idle_timeout=0.05)

    @server.widget
    def counter() -> TrackedWidget:
        return TrackedWidget()

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        assert closed.wait(1)
        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": launch.meta["anywidget"]["instanceId"],
                "operation_id": "poll-expired-session",
            },
        )

    assert poll.isError is True
    assert isinstance(poll.content[0], TextContent)
    assert "Unknown widget session" in poll.content[0].text


@pytest.mark.anyio
async def test_poll_and_comm_refresh_the_session_deadline() -> None:
    closed = threading.Event()

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            super().close()
            closed.set()

    server = AnyWidgetMCP("test", session_idle_timeout=1.0)

    @server.widget
    def counter() -> TrackedWidget:
        return TrackedWidget()

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]

        await anyio.sleep(0.4)
        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": payload["instanceId"],
                "operation_id": "refresh-deadline-poll",
            },
        )
        await anyio.sleep(0.4)
        comm = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": payload["instanceId"],
                "model_id": payload["rootModelId"],
                "operation_id": "refresh-deadline-comm",
                "data": {"method": "request_state"},
            },
        )
        await anyio.sleep(0.4)

        assert poll.isError is False
        assert comm.isError is False
        assert not closed.is_set()
        assert closed.wait(1.5)


@pytest.mark.anyio
async def test_multiple_sessions_share_one_expiry_thread() -> None:
    server = AnyWidgetMCP("test", session_idle_timeout=1.0)
    server.widget(CounterWidget)

    async with connected(server) as client:
        first = await client.call_tool("counter_widget", {})
        expiry_thread = server._widget_tools._expiry_thread
        second = await client.call_tool("counter_widget", {})

        assert first.isError is False
        assert second.isError is False
        assert expiry_thread is not None
        assert server._widget_tools._expiry_thread is expiry_thread
        assert expiry_thread.is_alive()

    assert not expiry_thread.is_alive()


@pytest.mark.anyio
async def test_active_poll_is_not_expired_mid_projection() -> None:
    server = AnyWidgetMCP("test", session_idle_timeout=0.05)
    created: list[CounterWidget] = []
    projections = 0

    def project(widget: AnyWidget) -> dict[str, int]:
        nonlocal projections
        assert isinstance(widget, CounterWidget)
        projections += 1
        if projections > 1:
            time.sleep(0.15)
        return {"value": widget.value}

    @server.widget(state=project)
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        instance_id = launch.meta["anywidget"]["instanceId"]
        created[0].value = 1

        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": instance_id,
                "operation_id": "slow-projection-poll",
            },
        )

        assert poll.isError is False
        assert instance_id in server._widget_tools._sessions
        followup = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": instance_id,
                "operation_id": "slow-projection-followup",
            },
        )
        assert followup.isError is False


@pytest.mark.anyio
async def test_expiry_thread_continues_when_one_widget_close_fails() -> None:
    closed = threading.Event()

    class BrokenCloseWidget(CounterWidget):
        def close(self) -> None:
            fail = getattr(self, "_fail_close", True)
            self._fail_close = False
            super().close()
            if fail:
                raise RuntimeError("close failed")

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            super().close()
            closed.set()

    server = AnyWidgetMCP("test", session_idle_timeout=0.05)

    @server.widget
    def broken() -> BrokenCloseWidget:
        return BrokenCloseWidget()

    @server.widget
    def tracked() -> TrackedWidget:
        return TrackedWidget()

    async with connected(server) as client:
        first = await client.call_tool("broken", {})
        second = await client.call_tool("tracked", {})

        assert first.isError is False
        assert second.isError is False
        assert closed.wait(1)


@pytest.mark.anyio
async def test_reused_root_is_rejected_without_closing_the_live_session() -> None:
    server = AnyWidgetMCP("test")
    widget = CounterWidget()

    @server.widget
    def counter() -> CounterWidget:
        return widget

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]
        reused = await client.call_tool("counter", {})
        comm = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": payload["instanceId"],
                "model_id": payload["rootModelId"],
                "operation_id": "reused-root-live-session",
                "data": {"method": "request_state"},
            },
        )

    assert reused.isError is True
    assert isinstance(reused.content[0], TextContent)
    assert "Return a fresh root" in reused.content[0].text
    assert comm.isError is False


@pytest.mark.anyio
async def test_reused_child_is_rejected_without_closing_the_live_session() -> None:
    server = AnyWidgetMCP("test")
    child = CounterWidget()
    roots: list[ParentWidget] = []

    @server.widget
    def parent() -> ParentWidget:
        root = ParentWidget(child=child)
        roots.append(root)
        return root

    async with connected(server) as client:
        launch = await client.call_tool("parent", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]
        reused = await client.call_tool("parent", {})
        comm = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": payload["instanceId"],
                "model_id": child.model_id,
                "operation_id": "reused-child-live-session",
                "data": {"method": "request_state"},
            },
        )

        assert roots[1].comm is None
        assert reused.isError is True
        assert isinstance(reused.content[0], TextContent)
        assert "fresh nested widgets" in reused.content[0].text
        assert comm.isError is False


def test_widget_rejects_ambiguous_factory_signatures() -> None:
    server = AnyWidgetMCP("test")

    def positional(value: int, /) -> CounterWidget:
        return CounterWidget(value=value)

    def variadic(*values: int) -> CounterWidget:
        return CounterWidget(value=sum(values))

    def arbitrary(**values: int) -> CounterWidget:
        return CounterWidget(value=sum(values.values()))

    for factory in (positional, variadic, arbitrary):
        with pytest.raises(TypeError, match="must"):
            server.widget(factory)

    server.close()


@pytest.mark.anyio
async def test_widget_reports_invalid_factory_return() -> None:
    server = AnyWidgetMCP("test")

    @server.widget
    def broken() -> Any:
        return object()

    async with connected(server) as client:
        result = await client.call_tool("broken", {})

    assert result.isError is True
    assert isinstance(result.content[0], TextContent)
    assert (
        "Widget factory returned object, expected AnyWidget" in result.content[0].text
    )
