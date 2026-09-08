# Expose widgets with WebMCP

`webmcp.enable()` exposes live [AnyWidgets](https://anywidget.dev/) to browser
agents through [WebMCP](https://webmachinelearning.github.io/webmcp/), a browser
API for page-provided tools. The notebook host keeps its existing widget
connection. Python can run on your machine, on a remote kernel, or in
[Pyodide](https://pyodide.org/) inside the browser.

Install the base package in the environment that owns your widgets:

```sh
pip install anywidget-mcp
```

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

Current and future `anywidget.AnyWidget` instances are instrumented. When a
widget renders, its page registers a state-reading tool and an update tool for
the traits that have writable input schemas. Repeated calls leave the enabled
session unchanged.

Use a browser with WebMCP enabled. Chrome documents
[local development and its origin trial](https://developer.chrome.com/docs/ai/webmcp).
Browsers where the API is unavailable continue rendering the widgets and report
the missing capability in the console.

## Create a widget

Run this in another notebook cell:

```python
import anywidget
import traitlets as t


class Counter(anywidget.AnyWidget):
    value = t.Int(0, min=0, max=100, help="Counter value").tag(sync=True)
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
`{"state": {"value": 7}}`. Clicking the button changes the same Python widget.
Separate instances receive separate tool identities. Multiple views of one
instance share its tools in the same page.

## Choose exposed state

Public synchronized traits are readable. Framework traits such as `layout`,
`tooltip`, and names beginning with `_` stay outside the tool result. Exclude a
trait explicitly with metadata:

```python
private_notes = t.Unicode().tag(sync=True, webmcp=False)
```

Writable inputs are inferred from JSON-compatible trait types. Their descriptions,
numeric bounds, and enumerated values appear in the input schema. Python traitlets
remains authoritative for validation, including custom validators and observers.
Adding synchronized traits refreshes the tools' input schemas automatically.
Read-only traits and traits with custom serialization are readable but do not
receive inferred update inputs.

State responses use an aggregate 8,000-byte JSON budget. Large values and binary
data produce bounded summaries. Updating a trait may trigger widget-defined side
effects through observers. Enable instrumentation for widgets whose public
synchronized state you intend browser agents to read and modify.

Cancellation stops waiting for a tool result. An update already dispatched to
Python may still complete. Read the widget state before deciding whether to
retry a cancelled update.

## Lifecycle and host permissions

Tools remain registered while at least one view is rendered. Source reloads
preserve WebMCP tools. Disable WebMCP with:

```python
webmcp.disable()
```

This restores current widget source serialization and stops instrumenting new
instances. Widgets remain open and interactive. Calling `webmcp.enable()`
again enables tools for current and future widgets.

`webmcp.is_enabled()` reports whether the Python session has WebMCP enabled.
Browser tool availability also depends on rendering and host permissions.
`enable()` and `disable()` return `None` and are idempotent.

The adapter registers tools in the outermost accessible same-origin document.
This lets a notebook embedded in a same-origin frame expose tools at the page
level. Cross-origin frames require the host's WebMCP `tools` permission, and
client discovery rules still apply. See
[Chrome's iframe permissions](https://developer.chrome.com/docs/ai/webmcp/imperative-api#cross-origin_iframes).

The host must permit execution of widget modules and their imports under its
[Content Security Policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/CSP).
Instrumentation uses the host's existing model connection for reads and writes.

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
Use `webmcp=False` on traits that browser agents should not read or modify.
