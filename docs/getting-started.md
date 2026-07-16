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

[Wigglystuff](https://koaning.github.io/wigglystuff/) is a rich library of
expressive AnyWidgets. Install it to follow the examples in this guide:

```sh
pip install wigglystuff
```

The [AnyWidget gallery](https://try.anywidget.dev/) contains many other widget
packages that can be registered through the same class or factory API.

## Serve a Wigglystuff widget

Start a streamable HTTP server for `ColorPicker`:

```sh
anywidget-mcp serve wigglystuff:ColorPicker
```

The MCP endpoint is `http://127.0.0.1:8000/mcp`. Each tool invocation owns a
new `ColorPicker` instance.

Inspect the tool contract before starting the server:

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
    _esm = """
    function render({ model, el }) {
      const button = document.createElement("button");
      const update = () => {
        button.textContent = `Count: ${model.get("value")}`;
      };
      button.addEventListener("click", () => {
        model.set("value", model.get("value") + 1);
        model.save_changes();
      });
      model.on("change:value", update);
      update();
      el.append(button);
      return () => model.off("change:value", update);
    }
    export default { render };
    """

    value = traitlets.Int(0).tag(sync=True)
```

Serve the class from the module:

```sh
anywidget-mcp serve counter:Counter
```

A button click updates `Counter.value` through the widget's normal comm path.
The default model-visible state includes `value`.

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
