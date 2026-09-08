from __future__ import annotations

import functools
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.client import Client
from mcp.types import Icon, TextContent, TextResourceContents, ToolAnnotations
from traitlets import Int
from wigglystuff import ColorPicker, Slider2D, SortableList

from anywidget_mcp import (
    APP_RESOURCE_URI,
    AppCSP,
    AnyWidgetMCP,
    WidgetTools,
    attach,
)
from anywidget_mcp._targets import describe_widget_target

from ._server_support import (
    CounterWidget,
    bootstrap_id,
    bootstrap_runtime,
    connected,
    state_id,
)

RowValues = list[str]


@pytest.mark.anyio
async def test_attach_registers_classes_sync_factories_and_async_factories() -> None:
    mcp = MCPServer("test")
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

    async with Client(
        mcp,
        raise_exceptions=True,
    ) as client:
        color = await client.call_tool("color_picker", {"color": "#c026d3"})
        color_runtime = await bootstrap_runtime(client, color)
        sync_result = await client.call_tool("counter", {"value": 3})
        async_result = await client.call_tool("async_counter", {"value": 4})

    assert color.structured_content == {
        "tool": "color_picker",
        "state": {"color": "#c026d3"},
        "state_id": state_id(color),
    }
    assert color.content == [
        TextContent(
            type="text",
            text=(
                'Opened Color Picker with state {"color":"#c026d3"}. '
                "To read later user changes, call anywidget_state with "
                f'{{"state_id":"{state_id(color)}"}}.'
            ),
        ),
        TextContent(
            type="text",
            text=f"urn:anywidget-mcp:bootstrap:{bootstrap_id(color)}",
        ),
    ]
    assert color.meta is not None
    assert color.meta["ui"] == {"resourceUri": APP_RESOURCE_URI}
    assert bootstrap_id(color) != color_runtime["instanceId"]
    assert state_id(color) not in {bootstrap_id(color), color_runtime["instanceId"]}
    assert color_runtime["sessionIdleTimeoutMs"] == 900_000
    assert color_runtime["context"] == {
        "version": 1,
        "tool": "color_picker",
        "state": {"color": "#c026d3"},
    }
    assert sync_result.structured_content == {
        "tool": "counter",
        "state": {"doubled": 6, "value": 3},
        "state_id": state_id(sync_result),
    }
    assert async_result.structured_content == {
        "tool": "async_counter",
        "state": {"doubled": 8, "value": 4},
        "state_id": state_id(async_result),
    }
    assert all(widget.comm is None for widget in created)


def test_attach_rejects_a_second_adapter() -> None:
    mcp = MCPServer("test")
    attach(mcp)

    with pytest.raises(ValueError, match="already attached"):
        attach(mcp)


def test_attach_rejects_an_existing_streamable_http_app() -> None:
    mcp = MCPServer("test")
    mcp.streamable_http_app()

    with pytest.raises(
        ValueError,
        match=r"attach\(\) must run before streamable_http_app\(\)",
    ):
        attach(mcp)


@pytest.mark.anyio
async def test_attach_rejects_an_occupied_app_resource_without_registering_tools() -> (
    None
):
    mcp = MCPServer("test")

    @mcp.resource(APP_RESOURCE_URI)
    def existing_app() -> str:
        return "existing"

    with pytest.raises(ValueError, match="already defines the app resource"):
        attach(mcp)

    async with Client(
        mcp,
        raise_exceptions=True,
    ) as client:
        resources = (await client.list_resources()).resources
        tools = (await client.list_tools()).tools

    assert [resource.uri for resource in resources] == ["ui://anywidget-mcp/app.html"]
    assert tools == []


@pytest.mark.anyio
async def test_attach_rejects_an_invalid_app_uri_without_poisoning_retry() -> None:
    mcp = MCPServer("test")

    with pytest.raises(ValueError, match="Invalid app_uri"):
        attach(mcp, app_uri="not a uri")

    assert isinstance(attach(mcp), WidgetTools)

    async with Client(
        mcp,
        raise_exceptions=True,
    ) as client:
        resources = (await client.list_resources()).resources
        tools = {tool.name for tool in (await client.list_tools()).tools}

    assert [resource.uri for resource in resources] == ["ui://anywidget-mcp/app.html"]
    assert {
        "anywidget_bootstrap",
        "anywidget_read",
        "anywidget_write",
        "anywidget_comm",
        "anywidget_poll",
        "anywidget_state",
        "anywidget_dispose",
        "anywidget_cancel",
    }.issubset(tools)


@pytest.mark.anyio
async def test_describe_widget_target_matches_registered_input_schema() -> None:
    description = describe_widget_target(
        ColorPicker,
        name="choose_color",
        title="Choose Color",
        description="Select a hexadecimal color.",
    )
    mcp = MCPServer("test")
    widgets = attach(mcp)
    widgets.widget(
        ColorPicker,
        name="choose_color",
        title="Choose Color",
        description="Select a hexadecimal color.",
    )
    async with Client(
        mcp,
        raise_exceptions=True,
    ) as client:
        tool = next(
            tool
            for tool in (await client.list_tools()).tools
            if tool.name == "choose_color"
        )

    assert description.target_name == "ColorPicker"
    assert description.tool_name == "choose_color"
    assert description.title == "Choose Color"
    assert description.description == "Select a hexadecimal color."
    assert description.kind == "widget-class"
    assert description.input_schema == tool.input_schema
    assert tool.description == description.description


@pytest.mark.anyio
async def test_widget_tool_owns_the_optional_loading_message() -> None:
    server = AnyWidgetMCP("test")
    received: list[int] = []

    @server.widget(title="Embedding Atlas")
    def create_atlas(row_count: int) -> CounterWidget:
        received.append(row_count)
        return CounterWidget(value=row_count)

    async with connected(server) as client:
        tool = next(
            tool
            for tool in (await client.list_tools()).tools
            if tool.name == "create_atlas"
        )
        result = await client.call_tool(
            "create_atlas",
            {
                "row_count": 12,
                "loading_message": "  Mapping 12 text rows…  ",
            },
        )
        runtime = await bootstrap_runtime(client, result)

    assert tool.input_schema["properties"]["loading_message"] == {
        "default": "Initializing Embedding Atlas…",
        "description": (
            "Progress text shown while this widget initializes. Describe the "
            "current request in at most 120 characters."
        ),
        "title": "Loading Message",
        "type": "string",
    }
    assert tool.input_schema["required"] == ["row_count"]
    assert received == [12]
    assert runtime["loadingMessage"] == "Mapping 12 text rows…"


@pytest.mark.anyio
async def test_target_loading_message_parameter_keeps_its_python_contract() -> None:
    server = AnyWidgetMCP("test")
    received: list[str] = []

    @server.widget(title="Code Walkthrough")
    def walkthrough(
        loading_message: str = "Preparing the walkthrough…",
    ) -> CounterWidget:
        received.append(loading_message)
        return CounterWidget()

    async with connected(server) as client:
        default_result = await client.call_tool("walkthrough", {})
        explicit_result = await client.call_tool(
            "walkthrough",
            {"loading_message": "Tracing quicksort…"},
        )
        default_runtime = await bootstrap_runtime(client, default_result)
        explicit_runtime = await bootstrap_runtime(client, explicit_result)

    assert received == ["Preparing the walkthrough…", "Tracing quicksort…"]
    assert default_runtime["loadingMessage"] == "Preparing the walkthrough…"
    assert explicit_runtime["loadingMessage"] == "Tracing quicksort…"


@pytest.mark.parametrize(
    "loading_message",
    ["Loading\u202eexe", "x" * 121, " \n "],
)
@pytest.mark.anyio
async def test_widget_loading_message_falls_back_for_invalid_display_text(
    loading_message: str,
) -> None:
    server = AnyWidgetMCP("test")

    @server.widget(title="Code Walkthrough")
    def walkthrough() -> CounterWidget:
        return CounterWidget()

    async with connected(server) as client:
        result = await client.call_tool(
            "walkthrough",
            {"loading_message": loading_message},
        )
        runtime = await bootstrap_runtime(client, result)

    assert runtime["loadingMessage"] == "Initializing Code Walkthrough…"


@pytest.mark.parametrize("kind", ["callable", "partial"])
@pytest.mark.anyio
async def test_compiled_factory_schema_matches_registration(kind: str) -> None:
    def make_counter(value: int = 1) -> CounterWidget:
        return CounterWidget(value=value)

    class CounterFactory:
        def __call__(self, value: int = 1) -> CounterWidget:
            return CounterWidget(value=value)

    factory = (
        CounterFactory()
        if kind == "callable"
        else functools.partial(
            make_counter,
            value=2,
        )
    )
    description = describe_widget_target(factory)
    mcp = MCPServer("test")
    widgets = attach(mcp)
    widgets.widget(factory)
    async with Client(
        mcp,
        raise_exceptions=True,
    ) as client:
        tool = next(
            tool
            for tool in (await client.list_tools()).tools
            if tool.name == description.tool_name
        )

    assert description.input_schema == tool.input_schema


@pytest.mark.anyio
async def test_explicit_empty_description_matches_registration() -> None:
    description = describe_widget_target(CounterWidget, description="")
    mcp = MCPServer("test")
    widgets = attach(mcp)
    widgets.widget(CounterWidget, description="")
    async with Client(
        mcp,
        raise_exceptions=True,
    ) as client:
        tool = next(
            tool
            for tool in (await client.list_tools()).tools
            if tool.name == "counter_widget"
        )

    assert description.description == ""
    assert tool.description == ""


@pytest.mark.anyio
async def test_compiled_factory_resolves_target_module_annotations() -> None:
    def row_counter(rows: RowValues) -> CounterWidget:
        return CounterWidget(value=len(rows))

    description = describe_widget_target(row_counter)
    mcp = MCPServer("test")
    widgets = attach(mcp)
    widgets.widget(row_counter)
    async with Client(
        mcp,
        raise_exceptions=True,
    ) as client:
        tool = next(
            tool
            for tool in (await client.list_tools()).tools
            if tool.name == "row_counter"
        )

    assert description.input_schema == tool.input_schema
    assert tool.input_schema["properties"]["rows"] == {
        "items": {"type": "string"},
        "title": "Rows",
        "type": "array",
    }


@pytest.mark.anyio
async def test_user_lifespan_wraps_sequential_widget_lifespans() -> None:
    events: list[str] = []
    cycle = 0

    @asynccontextmanager
    async def user_lifespan(
        _server: MCPServer,
    ) -> AsyncGenerator[dict[str, int], None]:
        nonlocal cycle
        cycle += 1
        current = cycle
        events.append(f"user {current} enter")
        try:
            yield {"cycle": current}
        finally:
            events.append(f"user {current} exit")

    server = AnyWidgetMCP("test", lifespan=user_lifespan)

    class TrackedWidget(CounterWidget):
        cycle = Int(0)

        def close(self) -> None:
            if self.comm is not None:
                events.append(f"widget {self.cycle} close")
            super().close()

    @server.widget
    def counter() -> TrackedWidget:
        events.append(f"widget {cycle} create")
        return TrackedWidget(cycle=cycle)

    for current in (1, 2):
        async with connected(server) as client:
            launch = await client.call_tool("counter", {})
            assert launch.is_error is False
            events.append(f"client {current} active")

    assert events == [
        "user 1 enter",
        "widget 1 create",
        "client 1 active",
        "widget 1 close",
        "user 1 exit",
        "user 2 enter",
        "widget 2 create",
        "client 2 active",
        "widget 2 close",
        "user 2 exit",
    ]


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
    properties = tool.input_schema["properties"]
    assert tool.title == "Counter"
    assert tool.input_schema["required"] == ["label"]
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


@pytest.mark.anyio
async def test_widget_classes_expose_filtered_constructor_schemas() -> None:
    server = AnyWidgetMCP("test")
    server.widget(ColorPicker)
    server.widget(SortableList)
    server.widget(Slider2D)

    async with connected(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert {
        name
        for name, tool in tools.items()
        if tool.meta == {"ui": {"resourceUri": "ui://anywidget-mcp/app.html"}}
    } == {"color_picker", "sortable_list", "slider_2d"}
    assert "kwargs" not in tools["color_picker"].input_schema["properties"]
    assert tools["sortable_list"].input_schema["required"] == ["value"]
    assert tools["sortable_list"].input_schema["properties"]["value"] == {
        "items": {"type": "string"},
        "title": "Value",
        "type": "array",
    }
    assert tools["slider_2d"].input_schema["properties"]["x_bounds"]["maxItems"] == 2
    assert tools["slider_2d"].input_schema["properties"]["x_bounds"]["minItems"] == 2
    assert tools["color_picker"].title == "Color Picker"
    assert tools["slider_2d"].title == "Slider 2D"


@pytest.mark.anyio
async def test_widget_class_description_uses_its_own_docstring() -> None:
    class DescribedCounterWidget(CounterWidget):
        """Track a synchronized counter."""

    server = AnyWidgetMCP("test")
    server.widget(DescribedCounterWidget)

    async with connected(server) as client:
        tool = next(
            tool
            for tool in (await client.list_tools()).tools
            if tool.name == "described_counter_widget"
        )

    assert tool.description == "Track a synchronized counter."


@pytest.mark.parametrize(
    "name",
    [
        "anywidget_bootstrap",
        "anywidget_read",
        "anywidget_write",
        "anywidget_comm",
        "anywidget_poll",
        "anywidget_state",
        "anywidget_dispose",
        "anywidget_cancel",
    ],
)
def test_widget_rejects_reserved_session_tool_names(name: str) -> None:
    server = AnyWidgetMCP("test")

    with pytest.raises(ValueError, match="reserved for the AnyWidget app"):
        server.widget(CounterWidget, name=name)


@pytest.mark.anyio
async def test_widget_rejects_duplicate_tool_names_and_preserves_the_first() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget, name="counter", title="First counter")

    with pytest.raises(ValueError, match="already registered"):
        server.widget(CounterWidget, name="counter", title="Second counter")

    async with connected(server) as client:
        tool = next(
            tool for tool in (await client.list_tools()).tools if tool.name == "counter"
        )

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


@pytest.mark.anyio
async def test_widget_metadata_preserves_annotations_icons_and_app_resource() -> None:
    server = AnyWidgetMCP("test")
    annotations = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    icons = [
        Icon(
            src="https://example.com/widget.svg",
            mime_type="image/svg+xml",
            sizes=["32x32"],
        )
    ]
    server.widget(
        CounterWidget,
        annotations=annotations,
        icons=icons,
    )

    async with connected(server) as client:
        tool = next(
            tool
            for tool in (await client.list_tools()).tools
            if tool.name == "counter_widget"
        )

    assert tool.annotations == annotations
    assert tool.icons == icons
    assert tool.meta == {"ui": {"resourceUri": "ui://anywidget-mcp/app.html"}}


@pytest.mark.anyio
async def test_session_tools_are_visible_to_the_app() -> None:
    server = AnyWidgetMCP("test")

    async with connected(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    for name in (
        "anywidget_bootstrap",
        "anywidget_read",
        "anywidget_write",
        "anywidget_comm",
        "anywidget_poll",
        "anywidget_dispose",
        "anywidget_cancel",
    ):
        assert tools[name].meta == {"ui": {"visibility": ["app"]}}
    state_tool = tools["anywidget_state"]
    assert state_tool.meta == {"ui": {"visibility": ["model"]}}
    assert state_tool.input_schema["required"] == ["state_id"]
    assert state_tool.annotations == ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    assert tools["anywidget_bootstrap"].input_schema["required"] == [
        "bootstrap_id",
        "operation_id",
    ]
    assert tools["anywidget_read"].input_schema["required"] == [
        "instance_id",
        "blob_id",
    ]
    assert tools["anywidget_dispose"].input_schema["required"] == ["session_id"]
    assert set(tools["anywidget_comm"].input_schema["properties"]) == {
        "instance_id",
        "model_id",
        "operation_id",
        "payload_ref",
        "acknowledged_operation_id",
    }
    assert "payload_ref" in tools["anywidget_comm"].input_schema["required"]
    assert "operation_id" in tools["anywidget_poll"].input_schema["required"]
    assert (
        "acknowledged_model_ids" in tools["anywidget_poll"].input_schema["properties"]
    )


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
        contents = (await client.read_resource(APP_RESOURCE_URI)).contents

    assert len(resources) == 1
    assert resources[0].mime_type == "text/html;profile=mcp-app"
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
    assert isinstance(contents[0], TextResourceContents)
    assert contents[0].mime_type == "text/html;profile=mcp-app"
    assert contents[0].meta == resources[0].meta


@pytest.mark.anyio
async def test_app_resource_preserves_opted_in_script_directives() -> None:
    csp: AppCSP = {"scriptDirectives": ["'wasm-unsafe-eval'", "'unsafe-eval'"]}
    server = AnyWidgetMCP("test", csp=csp)
    csp["scriptDirectives"].clear()

    async with connected(server) as client:
        resources = (await client.list_resources()).resources
        contents = (await client.read_resource(APP_RESOURCE_URI)).contents

    expected = {
        "resourceDomains": ["blob:"],
        "scriptDirectives": ["'wasm-unsafe-eval'", "'unsafe-eval'"],
    }
    assert resources[0].meta["ui"]["csp"] == expected
    assert contents[0].meta["ui"]["csp"] == expected


@pytest.mark.parametrize("directive", ["'unsafe-inline'", "wasm-unsafe-eval", None])
def test_app_resource_rejects_invalid_script_directives(directive: object) -> None:
    with pytest.raises(ValueError, match="scriptDirectives accepts"):
        AnyWidgetMCP("test", csp=cast(AppCSP, {"scriptDirectives": [directive]}))


def test_app_resource_requires_a_script_directive_list() -> None:
    with pytest.raises(TypeError, match="scriptDirectives must be a list"):
        AnyWidgetMCP("test", csp=cast(AppCSP, {"scriptDirectives": "'unsafe-eval'"}))


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


@pytest.mark.anyio
async def test_widget_reports_invalid_factory_return() -> None:
    server = AnyWidgetMCP("test")

    @server.widget
    def broken() -> Any:
        return object()

    async with connected(server) as client:
        result = await client.call_tool("broken", {})

    assert result.is_error is True
    assert isinstance(result.content[0], TextContent)
    assert (
        "Widget factory returned object, expected AnyWidget" in result.content[0].text
    )
