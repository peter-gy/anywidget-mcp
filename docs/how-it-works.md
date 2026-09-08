# How it works

An AnyWidget tool opens a fresh Python widget and renders its JavaScript
interface inside an [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview)
host. Browser interactions return to Python. The language model receives the
values selected by the tool's `state` option.

## Tool, widget, and model-visible state

```python
from anywidget_mcp.server import AnyWidgetMCP
from wigglystuff import ColorPicker

mcp = AnyWidgetMCP("Color tools")
mcp.widget(ColorPicker, state="color")
```

| Object                 | Role in the conversation                                       |
| ---------------------- | -------------------------------------------------------------- |
| Tool `color_picker`    | The model calls it to open an interface.                       |
| `ColorPicker` instance | Owns the Python values and browser presentation for that call. |
| Model-visible state    | Exposes the current `color` for the model's next answer.       |

Each call creates a separate widget session. Two calls to `color_picker` have
independent selections. A [factory](./factories) prepares an instance from tool
arguments, and a [sequence result](./composition#return-several-widgets-from-one-call)
opens several widgets in one session.

## Python defines the tool input

Registration derives the tool from a class or function:

| Python source                       | MCP tool contract                                        |
| ----------------------------------- | -------------------------------------------------------- |
| Class name                          | Snake-case name, such as `ColorPicker` to `color_picker` |
| Factory function name               | Tool name as written                                     |
| Docstring                           | Tool description                                         |
| Explicit parameters and annotations | Input names, types, required fields, and defaults        |
| `name=`, `title=`, `description=`   | Overrides for model-facing identity and host labels      |

Synchronized traits define live widget values. To accept an initial value as a
tool argument, declare it in a constructor or factory signature:

```python
@mcp.widget(state="color")
def pick_color(color: str = "#315efb") -> ColorPicker:
    """Open a color picker at the requested hexadecimal value."""
    return ColorPicker(color=color)
```

Registration adds an optional `loading_message` for the model to describe the
invocation while the interface opens. A target that declares that parameter
keeps its own input contract. The [API reference](./api#anywidgetmcp-widget)
describes status normalization and schema rules.

## Python owns synchronized state

1. The browser calls `model.set()` to change its local value, then
   `model.save_changes()` to send pending values to Python.
2. [Traitlets](https://traitlets.readthedocs.io/) validates the changes and runs
   Python observers. An observer can update other synchronized traits.
3. The app applies the resulting Python updates in order, then publishes one
   complete model-visible state projection.

A projection is the mapping selected for the model. It can be smaller than the
widget data: a plot can synchronize thousands of points while exposing its
selection and filter. [Share state with the model](./state) covers trait
selection and derived projections.

The initial tool result includes the projection and a `state_id`. After an
interaction, the app sends the latest projection through the host's
`updateModelContext` capability. The model can also call `anywidget_state` with
that ID to read the current projection. State handles expire with the session.

## The host renders the interface

The server supplies one HTML app resource. Widget-specific JavaScript and CSS
load into that app, which implements the
[AnyWidget frontend model API](https://anywidget.dev/en/afm/). The widget keeps
its notebook rendering, trait serialization, observers, binary values, custom
messages, and nested widget references.

Large sources, binary buffers, and JSON values use verified, chunked attachments.
The browser applies the complete transaction before publishing its state.
[Work with large widgets](./large-widgets) covers query-backed viewers and upload
capacity.

The MCP host controls the sandboxed iframe. External scripts, styles, frames,
and API calls need the matching [app policy](./deployment#configure-browser-and-app-policy).
Browser permissions and model-context delivery depend on the host's advertised
capabilities.

## A session owns its resources

The widget session includes the root widget and widgets referenced recursively
by its synchronized values. Python closes that graph when the app disposes the
session, its idle lifetime expires, or the server shuts down. A
[managed factory](./factories#hold-resources-for-the-session) keeps its resource
open until the graph closes.

An HTTP connection can reconnect while the widget session remains active.
Session data lives in the server process. Restarting the server ends its
sessions. See [Deployment](./deployment#session-lifetime) for lifetime settings.
