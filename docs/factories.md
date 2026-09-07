# Pass input to widgets

Use a typed Python function when the model should provide values before an
[AnyWidget](https://anywidget.dev/) opens. Its parameters define the tool input.
The server validates that input before it calls the function to create the
widget. This function is a widget factory.

The examples use widgets from
[Wigglystuff](https://koaning.github.io/wigglystuff/). You can use AnyWidgets
from any package, including widgets you author.

## Let the model choose the starting value

Register the function with `@mcp.widget`:

```python
from anywidget_mcp import AnyWidgetMCP
from wigglystuff import ColorPicker

mcp = AnyWidgetMCP("Color tools")


@mcp.widget(state="color")
def pick_color(color: str = "#315efb") -> ColorPicker:
    """Open a color picker at the requested hexadecimal value."""
    return ColorPicker(color=color)


mcp.run(transport="streamable-http")
```

When the model calls `pick_color`, the requested color becomes the picker's
starting value. You can change it in the conversation, then ask the model which
color you chose. `state="color"` exposes the picker's current color.

The explicit `color` parameter also defines the widget argument in the JSON
input schema. `anywidget-mcp` adds the optional
`loading_message` field, which a model can set to progress text shown in the
host while the widget initializes. The docstring becomes the tool description.

Use explicit named parameters for factory input. Positional parameters that
accept keyword arguments are supported. Positional-only, `*args`, and
`**kwargs` factory parameters raise `TypeError` during registration because
they cannot define object properties in the tool schema.

## Choose between a widget and a factory

Register an AnyWidget class directly when its constructor already matches the
input the model should provide. Use a widget factory to rename or validate
inputs, load data, configure the widget, or hold a resource while the widget is
active.

## Use the request context

Add each registration before the server's `mcp.run()` call.

A Python SDK
[`Context`](https://github.com/modelcontextprotocol/python-sdk/tree/v2.1.1)
parameter receives the active request context and stays
outside the generated tool input schema:

```python
from mcp.server.mcpserver import Context


@mcp.widget
async def prepared_picker(
    color: str = "#315efb",
    *,
    ctx: Context,
) -> ColorPicker:
    await ctx.info(f"Opening {color}")
    return ColorPicker(color=color)
```

Synchronous and asynchronous factories may return the widget directly.

## Hold resources for the session

A factory may return a synchronous or asynchronous context manager that yields
an AnyWidget or a non-empty sequence of AnyWidgets. The manager stays active
while the app session uses the returned widget graph. This integration
excerpt assumes your `DatasetExplorer` widget and asynchronous `open_dataset`
function. `open_dataset` returns an object with an `aclose()` method:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from mcp.server.mcpserver import Context

from my_widgets import DatasetExplorer, open_dataset


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
        with anyio.CancelScope(shield=True):
            await dataset.aclose()
```

App disposal, idle expiry, `aclose()`, and server shutdown close the widget
graph before the manager exits. Factory acquisition is cancellable. A manager
that acquires a resource before its final pre-yield await must protect cleanup
for that partial acquisition with `anyio.CancelScope(shield=True)`.

See [Model-visible state](./state) for projection choices and [API
reference](./api) for registration options.
