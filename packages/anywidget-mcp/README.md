# anywidget-mcp

`anywidget-mcp` exposes an AnyWidget class or factory as an interactive MCP
App tool. Each tool call creates a fresh widget, renders it in the host, and
keeps browser interaction synchronized with Python traitlets.

Install the adapter and the widget package you want to serve:

```sh
pip install anywidget-mcp wigglystuff
```

Expose an installed widget class:

```sh
anywidget-mcp serve wigglystuff:ColorPicker
```

The target may also be a synchronous or asynchronous factory:

```sh
anywidget-mcp serve my_widgets:create_picker
```

The command serves a streamable HTTP endpoint at
`http://127.0.0.1:8000/mcp`. Constructor parameters become the MCP tool input
schema, so the host can inspect and invoke the widget with typed arguments.

Inspect that contract before starting the server:

```sh
anywidget-mcp inspect wigglystuff:ColorPicker
anywidget-mcp inspect my_widgets:create_picker --json
```

## Serve one widget

Use `serve()` when one class or factory defines the server:

```python
from anywidget_mcp import serve
from wigglystuff import ColorPicker

serve(ColorPicker)
```

Set `transport="stdio"` when the MCP host launches the process:

```python
serve(ColorPicker, transport="stdio")
```

The command-line form accepts the same transport and server options:

```sh
anywidget-mcp serve wigglystuff:SortableList --port 8010
```

## Compose a widget tool server

`AnyWidgetMCP` extends `FastMCP`. Register each widget class or factory on
one server:

```python
from anywidget_mcp import AnyWidgetMCP
from wigglystuff import ColorPicker, SortableList

mcp = AnyWidgetMCP("Widget tools")
mcp.widget(ColorPicker, state="color")
mcp.widget(SortableList, state="value")
mcp.run()
```

Class names become snake-case tool names. `ColorPicker` registers
`color_picker`, and `Slider2D` registers `slider_2d`. Use `name=`,
`title=`, and `description=` when the host-facing contract needs another
label.

Every invocation creates a fresh root widget and recursively enrolls widget
references from synchronized dicts, lists, and tuples. AnyWidget subclasses
and descriptor-backed protocol objects use the same session. Repeated
references use one model, and container changes enroll new models before the
parent update reaches the browser.

`AnyWidgetMCP.run()` closes live sessions when its owned runtime exits. Call
`mcp.close()` when another application owns the server lifecycle.

## Attach to an existing FastMCP server

`attach()` adds widget tools to a server that already has other tools:

```python
from mcp.server.fastmcp import FastMCP

from anywidget_mcp import attach
from wigglystuff import ColorPicker

mcp = FastMCP("Existing tools")
widgets = attach(mcp)
widgets.widget(ColorPicker, state="color")
```

The embedding application calls `widgets.close()` during shutdown.

## Prepare widgets with factories

A factory can validate arguments, load data, or configure the widget before it
is rendered. Its explicit parameters define the tool schema:

```python
from anywidget_mcp import AnyWidgetMCP
from wigglystuff import ColorPicker

mcp = AnyWidgetMCP("Color tools")


@mcp.widget(state="color")
def pick_color(color: str = "#315efb") -> ColorPicker:
    """Open a color picker at the requested hexadecimal value."""
    return ColorPicker(color=color)
```

Async factories use the same decorator:

```python
@mcp.widget
async def prepared_picker(color: str = "#315efb") -> ColorPicker:
    return ColorPicker(color=color)
```

The class or factory signature is the complete input contract. A minimal
AnyWidget class whose constructor is `(*args, **kwargs)` produces an empty MCP
input schema. Register a factory with named parameters when callers need to
configure that widget.

## Choose model-visible state

The `state` option controls the concise widget state available to the model:

| Value                     | Projection                                              |
| ------------------------- | ------------------------------------------------------- |
| Omitted                   | Public synchronized root traits except display metadata |
| `"color"`                 | One selected root trait                                 |
| `("color", "show_label")` | Selected root traits                                    |
| `None`                    | State projection and model-context updates are disabled |
| `lambda widget: {...}`    | A custom mapping derived from the widget                |

The default omits the widget display traits `layout`, `tabbable`, and
`tooltip`.

A custom projection can expose an intent-focused view of a larger widget model:

```python
mcp.widget(
    SortableList,
    state=lambda widget: {
        "items": widget.value,
        "count": len(widget.value),
    },
)
```

Projection callables are read-only. Mutating synchronized widget traits while
building a projection returns a state-projection error.

When state projection is enabled, the tool result includes the initial
projection. Browser changes pass through each model's Python state handler.
When the host supports model-context updates, the app publishes the latest
complete projection through MCP Apps `updateModelContext` so later chat turns
can reason about the current widget state.

Tool text uses the registered title, such as `Opened Color Picker`. Structured
results and model context use the registered tool name through the `tool`
field.

Binary values become `{"type": "binary", "bytes": N}`. Large or recursive
values use deterministic bounded summaries. The complete widget graph, ESM,
CSS, buffers, and comm messages stay in app metadata.

## Configure the MCP App resource

`AnyWidgetMCP` accepts FastMCP options plus the app resource policy:

```python
mcp = AnyWidgetMCP(
    "Media tools",
    host="127.0.0.1",
    port=8000,
    csp={
        "connectDomains": ["https://api.example.com"],
        "resourceDomains": ["https://esm.sh"],
    },
    permissions={"camera": {}},
    cors_origins=["https://host.example.com"],
    session_idle_timeout=900,
)
```

The app resource uses `ui://anywidget-mcp/widget.html`. Add external ESM and
CSS origins to `resourceDomains`. Add API origins to `connectDomains`.

## Author widgets in marimo

Use marimo to build and inspect the AnyWidget class:

```python
import marimo as mo

from my_widgets import ColorPicker

picker = mo.ui.anywidget(ColorPicker())
picker
```

Expose the same class to an MCP host:

```sh
anywidget-mcp serve my_widgets:ColorPicker
```

The notebook and MCP App use the same widget implementation and trait contract.
