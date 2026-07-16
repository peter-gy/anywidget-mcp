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

## Return several widgets from one call

Return a non-empty sequence when one answer benefits from several interfaces:

```python
import anywidget
from wigglystuff import ColorPicker, SortableList


@mcp.widget
def review_palette(colors: list[str]) -> list[anywidget.AnyWidget]:
    """Choose a primary color and reorder the complete palette."""
    return [
        ColorPicker(color=colors[0]),
        SortableList(value=colors, editable=True),
    ]
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

## Turn an explanation into an interactive trace

[LiveEdit](https://koaning.github.io/wigglystuff/reference/live-edit/) renders a
step-through trace of a Python function. Its constructor already accepts the
runtime input a model should provide, so serve the widget class directly:

```sh
anywidget-mcp serve wigglystuff:LiveEdit --port 8010
```

`anywidget-mcp` derives the `live_edit` tool schema from the constructor. The
required `code` argument carries the Python source for each new widget.

::: warning
`LiveEdit` executes `code` in the MCP server process to collect the trace. Run
this example with trusted model output in an isolated development environment.
:::

Open [Inspector Chat](./getting-started#see-it-in-inspector-chat) and ask:

> Explain insertion sort for `[5, 2, 4, 1]`. Call `live_edit` with a
> self-contained, zero-argument Python function so I can step through it.

The model supplies Python source as ordinary tool input. Each call constructs a
fresh `LiveEdit` instance, and the user steps through the rendered trace in the
conversation. `LiveEdit` owns the tracing interface and can render through
AnyWidget support in Jupyter and marimo. `anywidget-mcp` supplies the MCP tool,
widget session, and host bridge.

## Create bespoke widgets at runtime

A factory can accept complete AnyWidget class definitions as tool input. The
package exports `create_anywidget` for constructing an interactive answer that
fits the current question.

::: danger Run generated code in a sandbox
`create_anywidget` executes the supplied Python with the MCP server's
permissions, then loads each widget's JavaScript in the app iframe. Run the
server inside a disposable sandbox with scoped filesystem, network, credential,
and process access.
:::

Serve the built-in factory on the Inspector endpoint:

```sh
anywidget-mcp serve anywidget_mcp:create_anywidget --port 8010
```

Pass `code` and an ordered `classnames` list. After the code executes, every
name must resolve to an AnyWidget subclass constructible without arguments.
Missing names and non-widget bindings fail the tool call. One selected class
renders directly. Several selected classes render as a vertical group in the
requested order. If `classnames` is omitted, `create_anywidget` selects the
last final namespace binding to a source-defined top-level AnyWidget class.

Open [Inspector Chat](./getting-started#see-it-in-inspector-chat) and ask:

> Our API retries rate-limited requests with exponential backoff. Create an
> interactive widget that lets me explore how the attempt count and base delay
> affect the retry schedule and total waiting time.

The model can call `create_anywidget` with `classnames=["RetryBudget"]` and a
self-contained class such as this retry-budget explorer:

::: details Python source passed to `create_anywidget`

```python
import anywidget
import traitlets


class RetryBudget(anywidget.AnyWidget):
    _esm = """
    function render({ model, el, signal }) {
      el.classList.add("retry-budget");
      el.innerHTML = `
        <header><strong>Retry budget</strong><span data-summary></span></header>
        <label>Attempts <output data-attempts-value></output>
          <input data-attempts type="range" min="2" max="6" step="1">
        </label>
        <label>Base delay <output data-base-value></output>
          <input data-base type="range" min="0.1" max="2" step="0.1">
        </label>
        <div data-schedule></div>`;

      const attempts = el.querySelector("[data-attempts]");
      const base = el.querySelector("[data-base]");

      function draw() {
        const count = model.get("attempts");
        const delay = model.get("base_delay");
        const waits = Array.from({ length: count }, (_, index) =>
          index === 0 ? 0 : delay * 2 ** (index - 1));
        const total = waits.reduce((sum, wait) => sum + wait, 0);
        attempts.value = count;
        base.value = delay;
        el.querySelector("[data-attempts-value]").textContent = count;
        el.querySelector("[data-base-value]").textContent = `${delay.toFixed(1)} s`;
        el.querySelector("[data-summary]").textContent = `${total.toFixed(1)} s total wait`;
        const max = Math.max(...waits, 0.1);
        el.querySelector("[data-schedule]").innerHTML = waits.map((wait, index) => `
          <div class="attempt"><span>Attempt ${index + 1}</span>
            <i style="width:${Math.max(3, 100 * wait / max)}%"></i>
            <output>${index ? `+${wait.toFixed(1)} s` : "now"}</output></div>`
        ).join("");
      }

      function update(name, value) {
        const number = Number(value);
        const count = name === "attempts" ? number : model.get("attempts");
        const delay = name === "base_delay" ? number : model.get("base_delay");
        model.set(name, number);
        model.set("total_wait", delay * (2 ** (count - 1) - 1));
        model.save_changes();
      }

      attempts.addEventListener("input", (event) =>
        update("attempts", event.target.value), { signal });
      base.addEventListener("input", (event) =>
        update("base_delay", event.target.value), { signal });
      model.on("change:attempts change:base_delay", draw);
      signal.addEventListener("abort", () =>
        model.off("change:attempts change:base_delay", draw), { once: true });
      draw();
    }
    export default { render };
    """

    _css = """
    .retry-budget {
      max-width: 520px; padding: 16px; border: 1px solid #d8e0eb;
      border-radius: 12px; background: #fff; color: #172033;
      font: 13px/1.4 ui-sans-serif, system-ui, sans-serif;
    }
    .retry-budget header {
      display: flex; justify-content: space-between; margin-bottom: 14px;
    }
    .retry-budget header strong { font-size: 16px; }
    .retry-budget header span, .retry-budget output { color: #596579; }
    .retry-budget label {
      display: grid; grid-template-columns: 1fr auto; gap: 6px; margin: 12px 0;
    }
    .retry-budget input {
      grid-column: 1 / -1; width: 100%; accent-color: #315efb;
    }
    .retry-budget .attempt {
      display: grid; grid-template-columns: 72px 1fr 58px;
      align-items: center; gap: 10px; margin-top: 8px;
    }
    .retry-budget .attempt i {
      height: 8px; border-radius: 4px; background: #315efb;
    }
    .retry-budget .attempt output { text-align: right; }
    @media (prefers-color-scheme: dark) {
      .retry-budget {
        background: #151b26; color: #edf2f8; border-color: #354052;
      }
      .retry-budget header span, .retry-budget output { color: #aeb8c8; }
    }
    """

    attempts = traitlets.Int(4).tag(sync=True)
    base_delay = traitlets.Float(0.5).tag(sync=True)
    total_wait = traitlets.Float(3.5).tag(sync=True)
```

:::

Moving either slider sends `attempts`, `base_delay`, and `total_wait` through
the ordinary AnyWidget comm path. The latest values become model-visible state
for the next chat turn. The same `RetryBudget` class can be instantiated in a
Jupyter or marimo notebook while `anywidget-mcp` carries each generated
instance into MCP App hosts.

## Register several widget tools

Pass several `MODULE:OBJECT` targets to expose them through one endpoint:

```sh
anywidget-mcp serve \
  wigglystuff:ManimWeb \
  wigglystuff:ColorPicker \
  --port 8010
```

The command registers `manim_web` and `color_picker` in argument order. Use the
Python API when two targets derive the same tool name and assign explicit
`name=` values.

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
an AnyWidget or a non-empty sequence of AnyWidgets. The manager stays active
while the app session uses the returned widget graph:

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
