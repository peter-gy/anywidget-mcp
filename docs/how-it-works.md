# How it works

`anywidget-mcp` registers an AnyWidget class or factory as an MCP tool and serves
the shared [MCP App](https://modelcontextprotocol.io/extensions/apps/overview)
that renders its result. The widget keeps the same
[AnyWidget](https://anywidget.dev/) frontend module, synchronized traitlets,
validation, and observers it uses in Jupyter or marimo.

## One widget, four contracts

Consider a small counter:

```python
# counter.py
import anywidget
import traitlets

from anywidget_mcp import serve


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


if __name__ == "__main__":
    serve(Counter, state="value")
```

Run `python counter.py` to expose the widget through streamable HTTP. The class
defines four related contracts:

| Source                      | Contract                                              |
| --------------------------- | ----------------------------------------------------- |
| `Counter` and its docstring | MCP tool identity and description                     |
| `_esm` and optional `_css`  | Browser presentation                                  |
| Traits tagged `sync=True`   | Live AnyWidget model shared by Python and the browser |
| `state="value"`             | Concise state sent to the model                       |

These contracts travel together when the tool runs, but each controls a
different part of the experience.

## How Python maps to MCP

Registration derives the MCP tool contract from the class or factory:

| Python source                              | MCP destination                                      |
| ------------------------------------------ | ---------------------------------------------------- |
| AnyWidget class name                       | Snake-case tool name, such as `Counter` to `counter` |
| Factory function name                      | Tool name as written                                 |
| `name=`                                    | Explicit tool name                                   |
| Tool name                                  | Human-facing title, such as `counter` to `Counter`   |
| Target docstring                           | Tool description                                     |
| Explicit constructor or factory parameters | Tool input schema                                    |
| Parameter annotations and defaults         | JSON Schema types, required fields, and defaults     |
| `annotations=` and `icons=`                | Standard MCP tool metadata                           |
| `app_uri`                                  | Tool `_meta.ui.resourceUri`                          |
| `csp`, `permissions`, and `prefers_border` | MCP App resource metadata                            |

`title=` and `description=` override the derived title and target docstring.
Run the inspector command to see the exact contract before starting a server:

```sh
anywidget-mcp inspect counter:Counter --json
```

Traits do not become tool arguments. A class constructor or factory signature
defines the arguments supplied when the model invokes the tool. Use a factory
for runtime input. This alternative server module reuses `Counter`:

```python
# counter_factory.py
from typing import Annotated

from pydantic import Field

from anywidget_mcp import serve
from counter import Counter


def open_counter(
    start: Annotated[
        int,
        Field(ge=0, description="Initial counter value."),
    ] = 0,
) -> Counter:
    """Open an interactive counter at a requested starting value."""

    return Counter(value=start)


if __name__ == "__main__":
    serve(open_counter, state="value")
```

Inspect `counter_factory:open_counter` to see an optional integer `start` field
in the MCP input schema. `Field(description=...)` supplies parameter-level
guidance. The factory docstring remains the tool description. Traitlets `help=`
text remains Python metadata, so put model-facing invocation guidance in the
target docstring, `description=`, or annotated parameter fields.

## One shared MCP App resource

Each server registers one HTML resource at
`ui://anywidget-mcp/widget.html` by default. Its MIME type is
`text/html;profile=mcp-app`. Every widget tool points to that resource through
`_meta.ui.resourceUri`, which lets an MCP host preload and render the app in a
sandboxed iframe.

The HTML resource contains the generic AnyWidget runtime. Widget-specific
`_esm` and `_css` sources travel as content-addressed session assets. The app
verifies and loads those assets before it creates the frontend model. This lets
one server render different AnyWidget classes through the same resource.

Resource metadata controls the host boundary:

- `csp` lists the external origins available to scripts, styles, frames, and
  network requests.
- `permissions` requests browser capabilities such as camera or clipboard
  write access.
- `prefers_border` tells the host whether the app prefers a bordered container.

External JavaScript, CSS, images, and APIs must be allowed by the corresponding
resource policy. See [Deployment](./deployment#configure-browser-and-app-policy)
for the complete configuration.

## What happens when the tool runs

1. The MCP host discovers the tool and its `_meta.ui.resourceUri` link. It can
   fetch the shared app resource before the first invocation.
2. The model invokes the tool with arguments validated against its JSON input
   schema.
3. The class or factory creates a fresh widget. Managed factories remain open
   for the lifetime of that widget session.
4. Python captures the synchronized widget graph and binary buffers. It
   externalizes `_esm` and `_css` as content-addressed session assets and adds
   the initial messages and selected model-visible state.
5. The tool result gives the model concise text and optional projected state.
   Private `_meta.anywidget` data gives the bundled app its session and render
   payload.
6. The app verifies the widget sources, creates the frontend model graph,
   applies launch messages, initializes each module, and renders the root
   widget.
7. User interaction changes browser state. `model.save_changes()` sends the
   pending values through the AnyWidget comm path to Python.
8. Python applies Traitlets deserialization, validation, and observers. The
   resulting updates return to the browser before the latest state projection
   is published to the host.
9. Polling starts 500 milliseconds after activity and backs off through 1, 2,
   5, 10, and 15 seconds while idle. Browser interaction resets the next poll
   to 500 milliseconds.
10. App teardown closes the browser bindings, Python widget graph, and managed
    factory.

The initial result has separate fields for separate consumers:

| Result field           | Consumer       | Contents                                              |
| ---------------------- | -------------- | ----------------------------------------------------- |
| `content`              | Model and host | A concise launch message                              |
| `structuredContent`    | Model and host | Registered tool name and projected state when enabled |
| `_meta.ui.resourceUri` | MCP host       | App resource to render                                |
| `_meta.anywidget`      | Bundled app    | Session, model graph, messages, and source manifest   |

`_meta.anywidget` is the adapter protocol between the Python package and its
bundled app. Application code should use AnyWidget traits, messages, and the
`anywidget-mcp` registration APIs.

## Synchronized state and model context

Trait synchronization makes the widget work. Model context gives the language
model a concise, current description of the interaction.

| Declaration                        | Browser model                         | Default model context |
| ---------------------------------- | ------------------------------------- | --------------------- |
| Public trait tagged `sync=True`    | Included                              | Included              |
| `layout`, `tabbable`, or `tooltip` | Included                              | Excluded              |
| Trait whose name starts with `_`   | Included when synchronized            | Excluded              |
| Trait without `sync=True`          | Excluded                              | Excluded              |
| `_esm` and `_css`                  | Included and loaded as widget sources | Excluded              |

The default projection selects these traits before applying its bounded JSON
encoding and collection limits.

An explicit trait selection or projection can expose a different mapping to the
model. For example, a visualization may synchronize thousands of points for
rendering while publishing only its current selection and filter. Given an
existing `mcp` server and `ScatterPlot` class, the registration excerpt is:

```python
mcp.widget(
    ScatterPlot,
    state=lambda widget: {
        "selection": widget.selection,
        "filter": widget.filter,
    },
)
```

`state=None` disables model-context projection while synchronized traits keep
the widget interactive. When projection is enabled, the initial tool result
contains that state. After interaction, the app publishes complete replacement
snapshots through MCP Apps `updateModelContext` when the host supports it. See
[Model-visible state](./state) for trait selection, `StateProjection`, sequence
results, and projection limits.

## The AnyWidget contract stays portable

The frontend continues to use the standard AnyWidget model methods:

- `model.get()` reads a synchronized trait.
- `model.set()` changes browser state and fires local change listeners.
- `model.save_changes()` commits pending values to Python.
- `model.on()` and `model.off()` subscribe to trait and custom-message events.
- `model.send()` sends a custom message to Python.
- `host.getModel()` and `host.getWidget()` resolve nested widget references.

Trait serializers, validators, observers, binary values, custom messages, and
nested widget references keep their AnyWidget behavior. The same class can run
in a notebook and through an MCP host. A factory adds inference-time arguments
or session resources around that class.

Continue with [Factories and composition](./factories) for runtime inputs and
managed resources, or use the [API reference](./api) for registration defaults
and errors.
