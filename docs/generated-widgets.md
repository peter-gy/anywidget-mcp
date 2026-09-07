# Create widgets from source

`create_anywidget` accepts Python source and constructs AnyWidgets from its
final module namespace. Register it when a model should create an interface
for the current task.

::: danger Run generated code in a sandbox
Supplied Python executes with the MCP server's permissions. Run this server in
a disposable sandbox with scoped filesystem, network, credential, and process
access. Each widget's JavaScript runs in the host's app iframe.
:::

```sh
anywidget-mcp serve anywidget_mcp:create_anywidget --port 8010
```

Connect [Inspector Chat](./getting-started#see-it-in-inspector-chat) and ask:

> Create a counter widget with increment and decrement buttons. Expose its
> current value so we can discuss it after I interact with it.

The model supplies `code` and may select classes with `classnames`.

## Select classes by name

The same factory can run directly in Python:

```python
from anywidget_mcp import create_anywidget

code = """
import anywidget
import traitlets

class Counter(anywidget.AnyWidget):
    value = traitlets.Int(0).tag(sync=True)
    _esm = '''
    export default {
      render({ model, el, signal }) {
        const button = document.createElement("button");
        const draw = () => { button.textContent = model.get("value"); };
        button.addEventListener("click", () => {
          model.set("value", model.get("value") + 1);
          model.save_changes();
        }, { signal });
        model.on("change:value", draw);
        draw();
        el.append(button);
        return () => model.off("change:value", draw);
      }
    };
    '''
"""

widget = create_anywidget(code, classnames=["Counter"])
```

Names resolve in the namespace after the source finishes executing. Every
selected binding must be an AnyWidget subclass with a zero-argument
constructor. One class returns one widget. Several classes return widgets in
the requested order and [render together](./composition#return-several-widgets-from-one-call).

Omit `classnames` to select the last final binding to a source-defined,
top-level AnyWidget class. Use explicit names when source defines several
widgets or aliases. Missing names, invalid bindings, and constructor errors
fail the tool call.

Source can define helpers, imports, and several classes in the same module.
The module remains registered while the returned widgets remain reachable, so
methods retain their globals and Python can resolve their class annotations.

The [API reference](./api#create-anywidget) lists arguments and errors.
