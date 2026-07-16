# Factories and composition

A widget factory turns typed MCP tool input into a fresh
[AnyWidget](https://anywidget.dev/). Use one when a tool call needs to validate
arguments, load data, configure a widget, or hold a resource for the widget
session.

The examples use widgets from
[Wigglystuff](https://koaning.github.io/wigglystuff/). The factory and
composition APIs accept AnyWidget classes from other packages and widgets you
author yourself.

## Accept runtime input

Register a factory with `@mcp.widget`:

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

The factory signature becomes the JSON input schema for `pick_color`. Its
docstring becomes the tool description, and `state="color"` exposes the current
selection to the model.

Use explicit named parameters for factory input. Positional parameters that
accept keyword arguments are supported. Positional-only, `*args`, and
`**kwargs` factory parameters raise `TypeError` during registration because
they cannot define object properties in the tool schema.

## Register several widget tools

`AnyWidgetMCP` extends `FastMCP` and keeps ordinary tools and widget tools on
one server:

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
constructor already defines the intended tool schema.

## Attach to an existing server

`attach()` adds widget registration and session handling to an existing
`FastMCP` server:

```python
from mcp.server.fastmcp import FastMCP

from anywidget_mcp import attach
from wigglystuff import ColorPicker

mcp = FastMCP("Analysis tools")
widgets = attach(mcp)
widgets.widget(ColorPicker, state="color")
```

The adapter composes widget cleanup into the existing server lifespan. Use
`await widgets.aclose()` inside an active lifespan to close live widget
sessions early.

## Use the request context

A `FastMCP` `Context` parameter receives the active request context and stays
outside the generated tool input schema:

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

Synchronous and asynchronous factories may return the widget directly.

## Hold resources for the session

A factory may return a synchronous or asynchronous context manager that yields
an AnyWidget. The manager stays active while the app session uses the widget:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context

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
        await dataset.aclose()
```

App disposal, idle expiry, `aclose()`, and server shutdown close the widget
graph before the manager exits. Factory acquisition is cancellable. A manager
that acquires a resource before its final pre-yield await must protect cleanup
for that partial acquisition with `anyio.CancelScope(shield=True)`.

See [Model-visible state](./state) for projection choices and [API
reference](./api) for registration options.
