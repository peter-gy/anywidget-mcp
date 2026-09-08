# Expose widgets with WebMCP

`webmcp.enable()` lets browser agents read, update, and create
[AnyWidgets](https://anywidget.dev/) through
[WebMCP](https://webmachinelearning.github.io/webmcp/), a browser API for
page-provided tools. The notebook host keeps its existing widget
connection. Python can run on your machine, on a remote kernel, or in
[Pyodide](https://pyodide.org/) inside the browser.

Install the base package in the environment that owns your widgets:

```sh
pip install anywidget-mcp
```

For [JupyterLab](https://jupyterlab.readthedocs.io/), the notebook application,
install its Python kernel in the same environment and start the app:

```sh
pip install jupyterlab ipykernel
jupyter lab
```

Select that environment's Python kernel when opening a notebook. The WebMCP
session uses Jupyter's native widget manager to render and synchronize widgets.

In Pyodide, use its [micropip](https://micropip.pyodide.org/) package installer:

```python
import micropip

await micropip.install("anywidget-mcp")
```

Enable WebMCP in a notebook cell:

```python
from anywidget_mcp import webmcp

webmcp.enable()
```

Current and future `anywidget.AnyWidget` instances become available. The
returned session displays **WebMCP ready** and publishes read and update tools
for those instances. Existing widget views also publish their tools and keep
their normal rendering locations. Repeated `enable()` calls configure the same
active session.

Use a browser with WebMCP enabled. Chrome documents
[local development and its origin trial](https://developer.chrome.com/docs/ai/webmcp).
Browsers where the API is unavailable continue rendering the widgets and report
the missing capability in the console.

## Interact with an existing widget

Run this in another notebook cell.
[traitlets](https://traitlets.readthedocs.io/) validates the widget's Python
attributes:

```python
import anywidget
import traitlets as t


class Counter(anywidget.AnyWidget):
    value = t.Int(0, min=0, max=100, help="Counter value").tag(sync=True)
    notes = t.Unicode().tag(sync=True)
    _esm = """
    export default {
      render({ model, el, signal }) {
        const button = document.createElement('button');
        const update = () => { button.textContent = `Count: ${model.get('value')}`; };
        button.addEventListener('click', () => {
          model.set('value', model.get('value') + 1);
          model.save_changes();
        }, { signal });
        model.on('change:value', update);
        update();
        el.append(button);
        return () => model.off('change:value', update);
      }
    };
    """


counter = Counter()
counter
```

The page now offers **Read Counter** and **Update Counter**. Invoking the update
tool with `{"value": 7}` changes the button to **Count: 7** and returns
`{"state": {"value": 7, "notes": ""}}`. Clicking the button changes the same
Python widget.
Separate instances receive separate tool identities. Multiple views of one
instance share its tools in the same page.

## Let the agent create widgets

Pass classes and typed functions to `widgets=`. Registering them leaves them
uncalled:

```python
def create_counter(start: int = 0) -> Counter:
    """Create an interactive counter starting at the requested value."""
    return Counter(value=start)


webmcp.enable(widgets=[Counter, create_counter], discover=False)
```

Keep `enable(...)` as the cell's final expression, or assign its result and
display it:

```python
tools = webmcp.enable(widgets=[Counter, create_counter], discover=False)
tools
```

The session offers **Counter** and **Create Counter**. Calling **Create Counter**
with `{"start": 7}` constructs a fresh widget in Python and renders **Count: 7**
inside the session output. The tool returns `widget_id`, `ref`, `state`, and
`tools`, which contains its read tool name and its update tool name when writable
traits exist. The response resolves after rendering and tool registration.
Model lookup, rendering, and browser tool registration each time out after
10 seconds. A timeout cancels that preparation step and withdraws its partial
tool registrations. A failed browser registration can retry when the catalog
refreshes or another session view opens.

A class exposes its explicitly typed constructor parameters. `Counter` inherits
AnyWidget's generic constructor, so its creation tool accepts `{}`. Use
`create_counter` to accept `start`. Functions can be synchronous or asynchronous
and must return a fresh AnyWidget instance. Their names, docstrings, annotations,
and defaults define the tool contract. Input validation runs before the call.

Parameters must accept keyword arguments. Supported annotations are `str`,
`int`, `float`, `bool`, `None`, unions, `Literal`, scalar-valued `Enum`,
`list[T]`, and `dict[str, T]`. Missing annotations, positional-only function
parameters, variadic functions, and incompatible defaults produce registration
errors. Unknown arguments and incompatible values produce tool errors.

`discover=False` includes explicitly supplied instances and widgets created
through the session. `discover=True`, the initial default, also includes current
and future live instances. Discovery uses the live widget registry. Classes and
functions become creation tools when supplied explicitly.

## Choose exposed state

Use a mapping to configure individual targets:

```python
webmcp.enable(
    widgets={
        Counter: {"private": {"notes"}},
        create_counter: {"title": "Start a counter"},
        counter: {"traits": {"value"}, "read_only": True},
    },
    discover=True,
)
```

`Counter` instances hide `notes`. The existing `counter` exposes a read tool for
`value`, while Python code and its button can still change that value. The
function's creation tool appears as **Start a counter**.

| Setting                | Behavior                                                                                                     |
| ---------------------- | ------------------------------------------------------------------------------------------------------------ |
| `name`                 | Set the tool name prefix. Instance identities keep names unique. Use 1–64 letters, digits, `_`, `.`, or `-`. |
| `title`, `description` | Explain the target to the browser agent.                                                                     |
| `traits`               | Expose a set of synchronized trait names. `None` uses the default public traits.                             |
| `private`              | Exclude a set of traits from schemas, reads, updates, and returned state.                                    |
| `read_only`            | Use `True` for the whole widget or a set of traits. `False` uses inferred writable inputs.                   |

Class settings apply from base class to subclass, then the originating
function's settings apply, then instance settings. A more specific setting
replaces the same field, including a set of trait names. Python enforces the
resulting policy for every request.

Public synchronized traits are readable by default. Framework traits such as
`layout`, `tooltip`, and names beginning with `_` stay outside the tool result.
Declare a trait with `webmcp=False` to exclude it under every configuration:

```python
class PrivateCounter(Counter):
    notes = t.Unicode().tag(sync=True, webmcp=False)
```

WebMCP privacy controls tool exposure. A synchronized private trait still reaches
the widget frontend, and widget-defined derived values may reveal its contents.

Writable inputs are inferred from JSON-compatible trait types. Their descriptions,
numeric bounds, and enumerated values appear in the input schema. Python
traitlets remains authoritative, including custom validators and observers.
Adding synchronized traits refreshes the tools' input schemas automatically.
Traitlets read-only traits and traits with custom serialization are readable
but do not receive inferred update inputs.

State responses use an aggregate 8,000-byte JSON budget. Large values and binary
data produce bounded summaries. Updating a trait may trigger widget-defined side
effects through observers.

## Update configuration

Repeated `enable()` calls add targets and replace supplied configuration fields.
Omitted fields, including `discover`, retain their current values. A sequence is
shorthand for targets with empty settings. Unknown option names and unknown
traits on existing instances raise configuration errors. Traits on a creation
result are checked when the widget exists.

Withdraw an instance and keep discovery from including it:

```python
webmcp.enable(widgets={counter: False})
```

`False` also withdraws a class or function's creation tool and excludes its
instances or results. An exclusion at any applicable level takes precedence.
To include the target again, pass settings such as `{counter: {}}`.

## Lifecycle and host permissions

The displayed session publishes its configured instance and creation tools.
A widget's own rendered views also publish its instance tools. Multiple views
share registrations, and cleanup releases each view's registrations. Source
reloads preserve WebMCP tools.

Withdraw tools and stop discovery with:

```python
webmcp.disable()
```

Widgets remain open and interactive. A later `webmcp.enable()` starts a new
session. Retain the returned session when you want to close its created widgets:

```python
tools.close()
```

`close()` withdraws that session's tools and closes the widgets created through
it. Existing instances retain their owners. Removing a notebook output releases
its browser registrations while Python widgets remain alive.

`webmcp.is_enabled()` reports whether the Python session has active WebMCP
exposure. Browser tool availability also depends on rendering and host
permissions.

Cancellation stops waiting for a tool result. An update or creation already
dispatched to Python may still complete. Inspect the widget state or session
output before deciding whether to retry, especially when a call creates a widget.

The session requires the host's AnyWidget model and child-rendering APIs,
including `host.getWidget()`, available in AnyWidget 0.11+. Use a compatible
notebook frontend as well as the installed Python package.

The adapter registers tools in the outermost accessible same-origin document.
This lets a notebook embedded in a same-origin frame expose tools at the page
level. Cross-origin frames require the host's WebMCP `tools` permission, and
client discovery rules still apply. See
[Chrome's iframe permissions](https://developer.chrome.com/docs/ai/webmcp/imperative-api#cross-origin_iframes).

The host must permit execution of widget modules and their imports under its
[Content Security Policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/CSP).
Instrumentation uses the host's existing model connection for reads, writes,
and creation requests.

## Compose with MCP Apps

Install `anywidget-mcp[server]` to run an
[MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview) server.
Enable WebMCP before creating widgets:

```python
from anywidget_mcp import webmcp
from anywidget_mcp.server import serve

webmcp.enable()
serve(Counter)
```

The server owns its widget sessions. The WebMCP adapter uses the same model
connection as the rendered widget. Availability to a browser agent depends on
the MCP host's frame permissions and the client's discovery support.

WebMCP exposure follows the synchronized-trait rules on this page. The server's
`state=` option separately controls the state shared with the MCP conversation.
Configure WebMCP exposure with `enable(widgets=...)` or trait metadata
`webmcp=False`.
