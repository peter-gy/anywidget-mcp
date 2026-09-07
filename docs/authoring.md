# Write a widget

An [AnyWidget](https://anywidget.dev/) combines a Python class with a JavaScript
frontend module. Define synchronized values with
[Traitlets](https://traitlets.readthedocs.io/), the typed attributes that carry
state between Python and the browser.

Install the package in your Python environment:

```sh
uv pip install anywidget-mcp
```

## Create a counter

Save this as `counter.py`:

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

The click handler sets the browser value and calls `save_changes()` to send it
to Python. Traitlets validates the update and runs Python observers. The app
then applies the resulting Python state and shares the selected values with
the model.

The render function's abort signal ends with its view. Use it to release event
listeners, and return a cleanup function for resources that need explicit
teardown. [AnyWidget's frontend API](https://anywidget.dev/en/afm/) describes
rendering, initialization, and model methods.

## Run from Python

```python
from anywidget_mcp import serve
from counter import Counter

serve(Counter, state="value")
```

`serve()` listens on `http://127.0.0.1:8000/mcp` and blocks until the server
exits. [Connect an MCP Apps host](./getting-started#see-it-in-inspector-chat)
to use the counter in a conversation.

## Develop in a notebook

The same class works in Jupyter or
[marimo](https://docs.marimo.io/api/inputs/anywidget/), a reactive Python notebook:

```python
import marimo as mo
from counter import Counter

counter = mo.ui.anywidget(Counter())
counter
```

Install marimo separately for this workflow. Use a [factory](./factories) when
the model should provide the starting value, and [select state](./state) to
control what the model reads.
