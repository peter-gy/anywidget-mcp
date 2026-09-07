# Combine widgets and tools

Use one server when a conversation needs several interfaces. Register each
widget as a separate tool, return several widgets from one factory, or attach
widget tools to an existing MCP server.

## Register several widget tools

Pass several `MODULE:OBJECT` targets to expose them through one endpoint:

```sh
anywidget-mcp serve \
  wigglystuff:SortableList \
  wigglystuff:ColorPicker \
  --port 8010
```

The command registers `sortable_list` and `color_picker` in argument order. Use the
Python API when two targets derive the same tool name and assign explicit
`name=` values.

`AnyWidgetMCP` extends the Python SDK
[`MCPServer`](https://github.com/modelcontextprotocol/python-sdk/tree/v2.1.1),
which registers tools and serves the MCP protocol:

```python
from anywidget_mcp import AnyWidgetMCP
from wigglystuff import ColorPicker, SortableList

mcp = AnyWidgetMCP("Widget tools")
mcp.widget(ColorPicker, state="color")
mcp.widget(SortableList, state="value")
mcp.run(transport="streamable-http")
```

Widget class names become snake-case tool names. `ColorPicker` registers
`color_picker`, and `Slider2D` registers `slider_2d`. Set `name`, `title`, and
`description` when the host-facing contract needs different labels.

Registration accepts a class or factory. Pass a factory when each tool call
needs arguments that differ from the widget constructor. Pass a class when its
constructor parameters define the intended widget arguments.

## Attach to an existing server

`attach()` adds widget registration and session handling to an existing
`MCPServer` server:

```python
from mcp.server.mcpserver import MCPServer

from anywidget_mcp import attach
from wigglystuff import ColorPicker

mcp = MCPServer("Analysis tools")
widgets = attach(mcp)
widgets.widget(ColorPicker, state="color")
mcp.run(transport="streamable-http")
```

The adapter composes widget cleanup into the existing server lifespan. Use
`await widgets.aclose()` inside an active lifespan to close live widget
sessions early. Call `attach()` before creating the server's
`streamable_http_app()`.

## Return several widgets from one call

Register a factory that returns several interfaces. The input requires at
least one color:

```python
from typing import Annotated

import anywidget
from pydantic import Field
from anywidget_mcp import AnyWidgetMCP
from wigglystuff import ColorPicker, SortableList


mcp = AnyWidgetMCP("Palette tools")


@mcp.widget
def review_palette(colors: Annotated[list[str], Field(min_length=1)]) -> list[anywidget.AnyWidget]:
    """Choose a primary color and reorder the supplied palette."""
    return [
        ColorPicker(color=colors[0]),
        SortableList(value=colors, editable=True),
    ]


mcp.run(transport="streamable-http")
```

The widgets render vertically in sequence order and share one app session. When
state projection is enabled, a sequence result exposes one aggregate state
projection. A singleton sequence keeps the same shape:

```json
{
	"widgets": [
		{ "color": "#315efb", "show_label": true },
		{
			"addable": false,
			"editable": true,
			"label": "",
			"removable": false,
			"value": ["#315efb", "#172033"]
		}
	]
}
```

The `state` option applies to every returned widget. Selected trait names must
exist on every widget in the sequence. Custom projection callables run once
per widget, and `state=None` disables the aggregate projection. Projection
limits encode a large `widgets` list as a bounded sequence summary with `type`
and `length`. The summary may also include retained `items`, an `omitted` count,
and `jsonBytes`.
