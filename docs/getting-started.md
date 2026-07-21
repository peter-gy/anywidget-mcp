# Getting started

Run a `ColorPicker` in Inspector Chat, choose a color, and ask the model which
color you chose.

Requires Python 3.11 or newer.

## Run the ColorPicker

Start [Wigglystuff](https://koaning.github.io/wigglystuff/)'s `ColorPicker` in
one command:

```sh
uvx --with wigglystuff anywidget-mcp serve wigglystuff:ColorPicker --port 8010
```

The MCP endpoint is `http://127.0.0.1:8010/mcp`.

Browse the [AnyWidget gallery](https://try.anywidget.dev/) for more widgets and
links to their packages.

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
model provider, and ask: `Use color_picker so I can choose a color.` The picker
opens in the conversation. Choose a color, then ask: `Which color did I choose?`
The model reads the picker's current color when it answers. This value is the
widget's model-visible state.

If port 7878 is busy, use the Inspector URL printed in the terminal.

## Install in a project

Install `anywidget-mcp` in the Python environment that owns your widget code:

```sh
uv pip install anywidget-mcp
```

## Bring your own AnyWidget

Define a counter with the same Python state and frontend module you would use in
a notebook:

```python
# counter.py
import anywidget
import traitlets


class Counter(anywidget.AnyWidget):
    """Adjust a counter and inspect its current value."""

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

Point the CLI at the widget's import path:

```sh
anywidget-mcp serve counter:Counter
```

A button click updates `Counter.value` in Python. The default model-visible
state includes `value`.

## Use the same widget in marimo

Render `Counter` while developing it in a marimo notebook:

```python
import marimo as mo

from counter import Counter

counter = mo.ui.anywidget(Counter())
counter
```

The notebook and MCP App use the same widget code and synchronized `value`.

## Start the server from Python

Call `serve()` when the widget module should start the MCP server:

```python
from anywidget_mcp import serve
from counter import Counter

serve(Counter, state="value")
```

`serve()` listens on streamable HTTP by default and blocks until the transport
exits. Set `transport="stdio"` when the MCP host launches the process.

[How it works](./how-it-works) explains how the widget becomes an MCP tool, how
browser changes reach Python, and how the model reads the current value.

## Next steps

- [Model-visible state](./state) chooses which widget values the model can use.
- [Pass input to widgets](./factories) accepts values from the model, loads
  data, returns several widgets, and adds widgets to an existing MCP server.
- [Create AnyWidgets from source at runtime](./factories#create-anywidgets-from-source-at-runtime)
  covers generated widget code and its sandbox requirements.
- [Deployment](./deployment) covers tool inspection, HTTP and standard-input
  transports, browser policy, and session lifetime.
