# Getting started

`anywidget-mcp` registers an [AnyWidget](https://anywidget.dev/) class or factory
as an [MCP App](https://modelcontextprotocol.io/extensions/apps/overview) tool.
A tool invocation creates a fresh widget, renders it in the host, and keeps its
synchronized traits connected to Python.

Install Python 3.12 or newer and the adapter:

```sh
pip install anywidget-mcp
```

## Try an existing widget library

The `ColorPicker` examples use
[Wigglystuff](https://koaning.github.io/wigglystuff/), an AnyWidget library.
Install it with:

```sh
pip install wigglystuff
```

The [AnyWidget gallery](https://try.anywidget.dev/) lists other widget packages
that can be registered through the same class or factory API.

## Serve a Wigglystuff widget

Start a streamable HTTP server for `ColorPicker`:

```sh
anywidget-mcp serve wigglystuff:ColorPicker --port 8010
```

The MCP endpoint is `http://127.0.0.1:8010/mcp`. Each tool invocation owns a
new `ColorPicker` instance.

## See it in Inspector Chat

Keep the widget server running. In another terminal, start the
[mcp-use Inspector](https://mcp-use.com/docs/inspector):

```sh
npx --yes @mcp-use/inspector@12.0.3 \
  --url http://127.0.0.1:8010/mcp \
  --port 7878 \
  --no-open
```

Open [Inspector Chat](http://127.0.0.1:7878/inspector?tab=chat), configure a
model provider, and ask: `Use color_picker so I can choose a color.` The model
invokes the tool and the widget renders in Chat. Changing the color updates
Python traitlets and the model context used by later turns.

For an end-to-end browser check, keep `--no-open`, drive the same Chat prompt,
interact with the widget, and ask the model to report the selected color. If
port 7878 is busy, use the Inspector URL printed in the terminal.

## Serve several widget tools

Stop the `ColorPicker` server, then pass several targets to expose several tools
through the same endpoint:

```sh
anywidget-mcp serve \
  wigglystuff:ManimWeb \
  wigglystuff:ColorPicker \
  --port 8010
```

The command registers `manim_web` and `color_picker` in argument order. Calls to
each tool create their own widget sessions on the shared server.

Inspect a target's tool contract from another terminal:

```sh
anywidget-mcp inspect wigglystuff:ColorPicker
anywidget-mcp inspect wigglystuff:ColorPicker --json
```

Inspection reports the target kind, tool name, title, description, and JSON
input schema.

## Serve from Python

Use `serve()` when one class or factory defines the server:

```python
from anywidget_mcp import serve
from wigglystuff import ColorPicker

serve(ColorPicker, state="color")
```

`serve()` listens on streamable HTTP by default and blocks until the transport
exits. Set `transport="stdio"` when the MCP host launches the process.

## Serve your own AnyWidget

Define the widget with the same traitlets and frontend module used in a
notebook:

```python
# counter.py
import anywidget
import traitlets


class Counter(anywidget.AnyWidget):
    """Let the user adjust a counter and inspect its current value."""

    _esm = """
    function render({ model, el, signal }) {
      const button = document.createElement("button");
      const draw = () => {
        button.textContent = `Count: ${model.get("value")}`;
      };
      button.addEventListener("click", () => {
        model.set("value", model.get("value") + 1);
        model.save_changes();
      }, { signal });
      model.on("change:value", draw);
      signal.addEventListener("abort", () => {
        model.off("change:value", draw);
      }, { once: true });
      draw();
      el.append(button);
    }
    export default { render };
    """

    value = traitlets.Int(0, help="Current counter value.").tag(sync=True)
```

Serve the class from the module:

```sh
anywidget-mcp serve counter:Counter
```

A button click updates `Counter.value` through the AnyWidget comm path.
The default model-visible state includes `value`.

[How it works](./how-it-works) explains how the class name, docstring,
constructor, synchronized traits, and frontend sources map to the MCP tool,
shared app resource, browser model, and model context.

## Serve model-generated AnyWidgets

`create_anywidget(code, classnames=...)` constructs AnyWidget classes supplied
as tool input. The `code` argument executes with the server process permissions,
and each widget's JavaScript loads in the app iframe. Run this factory in a
sandbox with scoped filesystem, network, credential, and process access.

Serve the factory:

```sh
anywidget-mcp serve anywidget_mcp:create_anywidget --port 8010
```

Pass an ordered `classnames` list with the code. Each name must resolve to a
zero-argument AnyWidget class after execution. One selected class renders as
the root widget. Several selected classes render in the requested order through
the same MCP App result. If `classnames` is omitted, the last source-defined
top-level AnyWidget class binding in the final namespace is selected. Within
projection limits, the default model-visible state for several widgets is
`{"widgets": [state, ...]}`. Larger lists use a bounded sequence summary under
`widgets`.

See [Factories](./factories#create-anywidgets-from-source-at-runtime) for a
retry-budget explorer generated from a chat request.

## Develop in marimo

Render the same class while developing it in a marimo notebook:

```python
import marimo as mo

from counter import Counter

counter = mo.ui.anywidget(Counter())
counter
```

The notebook and MCP App use the same AnyWidget class and synchronized trait
contract. Use the [`state` option](./state) to control which values the MCP
host receives as model context.
