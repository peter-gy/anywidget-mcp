# anywidget-mcp

[![PyPI](https://img.shields.io/pypi/v/anywidget-mcp.svg)](https://pypi.org/project/anywidget-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/peter-gy/anywidget-mcp/blob/main/LICENSE)

`anywidget-mcp` exposes an [AnyWidget](https://anywidget.dev/) class or factory
as an interactive [MCP App](https://modelcontextprotocol.io/extensions/apps/overview)
tool. Each tool call creates a fresh widget, renders it in the host, and keeps
browser interaction synchronized with Python traitlets.

MCP Apps keep interactive interfaces inside the conversation. `anywidget-mcp`
packages the MCP tool, HTML resource, widget session, and model-context bridge
so the same widget can run in notebooks and MCP hosts.

[Read the documentation](https://peter-gy.github.io/anywidget-mcp/) for the
quickstart, factory lifecycle, state projections, and deployment options.

Install the adapter:

```sh
pip install anywidget-mcp
```

[Wigglystuff](https://koaning.github.io/wigglystuff/) is one concrete library
of expressive AnyWidgets. Install it to try a range of existing widgets:

```sh
pip install wigglystuff
```

Expose one of its widget classes:

```sh
anywidget-mcp serve wigglystuff:ColorPicker
```

The [AnyWidget gallery](https://try.anywidget.dev/) lists more reusable widget
packages that can be registered the same way.

The target may also be a factory that returns a widget directly or yields one
from a synchronous or asynchronous context manager:

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

Inspection reports `widget-class` or `factory` as the target kind. FastMCP
`Context` parameters are injected at call time and stay outside the displayed
input schema.

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
`color_picker`, and `Slider2D` registers `slider_2d`. Use `name=`, `title=`,
`description=`, `annotations=`, and `icons=` to define the host-facing tool
contract.

Every invocation creates a fresh root widget and recursively enrolls widget
references from synchronized dicts, lists, and tuples. AnyWidget subclasses
and descriptor-backed protocol objects use the same session. Repeated
references use one model, and container changes enroll new models before the
parent update reaches the browser.

`AnyWidgetMCP` composes widget cleanup into the FastMCP lifespan. Server
shutdown closes every widget session and exits each managed factory. Inside an
active server lifespan, `await mcp.aclose()` closes all sessions early and
stops accepting widget calls until the next lifespan starts.

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

`attach()` composes cleanup with the server's existing lifespan. Inside an
active lifespan, `await widgets.aclose()` closes all widget sessions early and
stops accepting widget calls until the next lifespan starts.

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

Async factories use the same decorator. A FastMCP `Context` parameter receives
the active request context and stays outside the MCP input schema:

```python
from mcp.server.fastmcp import Context


@mcp.widget
async def prepared_picker(
    color: str = "#315efb",
    *,
    ctx: Context,
) -> ColorPicker:
    await ctx.info(f"Opening {color}")
    return ColorPicker(color=color)
```

Use a context-managed factory when the widget owns a database, temporary file,
model, or another resource that must remain open for the session:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anywidget_mcp import AnyWidgetMCP
from mcp.server.fastmcp import Context

from my_widgets import DatasetExplorer, open_dataset

mcp = AnyWidgetMCP("Dataset tools")


@mcp.widget
@asynccontextmanager
async def explore_dataset(
    rows: list[str],
    ctx: Context,
) -> AsyncIterator[DatasetExplorer]:
    await ctx.report_progress(0, 2, "Preparing dataset")
    dataset = await open_dataset(rows)
    try:
        await ctx.report_progress(1, 2, "Opening explorer")
        yield DatasetExplorer(dataset=dataset)
    finally:
        await dataset.aclose()
```

Factories may return either context-manager type. Each manager must yield an
`AnyWidget`. The manager stays active until app disposal, idle expiry,
`aclose()`, or server shutdown. The widget graph closes before the manager
exits. The class or factory signature defines the input contract after FastMCP
removes its injected `Context` parameter. A minimal AnyWidget class whose
constructor is `(*args, **kwargs)` produces an empty input schema. Register a
factory with named parameters when callers need to configure that widget.

Cancellation interrupts factory acquisition. When a manager acquires a
resource before its final pre-yield await, shield that partial-acquisition
cleanup with `anyio.CancelScope(shield=True)`. Cleanup after a successful yield
runs inside the protected session teardown path. An awaitable factory owns the
same rollback until it returns its widget or manager.

## Describe tool behavior

`annotations` and `icons` use the standard MCP tool metadata types:

```python
from mcp.types import Icon, ToolAnnotations

mcp.widget(
    ColorPicker,
    state="color",
    annotations=ToolAnnotations(readOnlyHint=True),
    icons=[
        Icon(
            src="https://example.com/color-picker.svg",
            mimeType="image/svg+xml",
        )
    ],
)
```

`WidgetTools.widget()`, `AnyWidgetMCP.widget()`, and `serve()` accept both
options. `serve()` also applies `icons` to its MCP server.

## Choose model-visible state

The `state` option controls the concise widget state available to the model:

| Value                                     | Projection                                              |
| ----------------------------------------- | ------------------------------------------------------- |
| Omitted                                   | Public synchronized root traits except display metadata |
| `"color"`                                 | One selected root trait                                 |
| `("color", "show_label")`                 | Selected root traits                                    |
| `None`                                    | State projection and model-context updates are disabled |
| `lambda widget: {...}`                    | A custom mapping updated from the complete widget graph |
| `StateProjection(project, watch="value")` | A custom mapping updated by selected root traits        |

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

Use `StateProjection` to name the traits that can change that mapping:

```python
from anywidget_mcp import StateProjection


def list_summary(widget: SortableList) -> dict[str, object]:
    return {
        "items": widget.value,
        "count": len(widget.value),
    }


mcp.widget(
    SortableList,
    state=StateProjection(list_summary, watch="value"),
)
```

`watch=None` invalidates when any trait in the enrolled widget graph changes. A
string or sequence invalidates on those root traits. `watch=()` computes the
projection once during launch. Every `StateProjection` computes an initial
value.

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
values use deterministic bounded summaries. Runtime metadata carries the
widget graph, buffers, comm messages, protocol version, and content-addressed
references for ESM and CSS. The app verifies each source digest and caches the
source in a bounded memory cache and browser storage before rendering.
Versioned payloads use source references for `_esm` and `_css`. Browser storage
is best effort and never owns the render lifecycle.

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
