# How it works

Each widget tool call opens a fresh session in an MCP Apps host. The browser
renders the returned [AnyWidget](https://anywidget.dev/) or widget group and
sends your changes to Python. The model can read the current widget state.

The widget keeps the same frontend module, Python traits, validation, and
observers it uses in Jupyter or marimo. `anywidget-mcp` connects those pieces to
an [MCP App](https://modelcontextprotocol.io/extensions/apps/overview).

## One widget, four contracts

Consider a counter:

```python
# counter.py
import anywidget
import traitlets

from anywidget_mcp import serve


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
| `state="value"`             | Selected state sent to the model                      |

These contracts travel together when the tool runs. Each maps to a different
MCP or AnyWidget contract.

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
| Generated `loading_message`                | Optional app initialization status                   |
| `annotations=` and `icons=`                | MCP tool metadata                                    |
| `app_uri`                                  | Tool `_meta.ui.resourceUri`                          |
| `csp`, `permissions`, and `prefers_border` | MCP App resource metadata                            |

`title=` and `description=` override the derived title and target docstring.
Run the inspector command to see the generated contract before starting a
server:

```sh
anywidget-mcp inspect counter:Counter --json
```

Traits do not become tool arguments. A class constructor or factory signature
defines the domain arguments supplied when the model invokes the tool.
Registration also adds an optional `loading_message` string when the target has
no parameter with that name. The model can use it to describe the current
invocation, such as `"Preparing a counter at 12…"`. The adapter displays the
message while the app initializes and removes the generated argument before
calling the target.

When the target declares `loading_message`, its annotation and default define
that input field and the target receives its value. A valid string also becomes
the initialization status.

Use a factory for runtime input. This factory module reuses `Counter`:

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
`ui://anywidget-mcp/app.html` by default. Its MIME type is
`text/html;profile=mcp-app`. Every widget tool points to that resource through
`_meta.ui.resourceUri`, which lets an MCP host preload and render the app in a
sandboxed iframe.

The HTML resource contains the AnyWidget runtime. Widget-specific
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
for the policy options.

## What happens when the tool runs

1. The MCP host discovers the tool and its `_meta.ui.resourceUri` link. It can
   fetch the shared app resource before the first invocation.
2. The model invokes the tool with arguments validated against its JSON input
   schema. The host forwards complete tool input to the app, which displays
   `loading_message` or an initialization message derived from the tool title.
3. The adapter removes a generated `loading_message` argument. The class or
   factory creates a fresh widget. Managed factories remain open
   for the lifetime of that widget session.
4. Python captures the synchronized widget graph and binary buffers. It
   externalizes `_esm` and `_css` as content-addressed session assets and adds
   the initial messages and selected model-visible state.
5. The tool result gives the model a launch message and optional projected
   state. A separate text block gives the app a bootstrap capability in the
   exact form `urn:anywidget-mcp:bootstrap:<32-lowercase-hex-capability>`.
6. The app calls `anywidget_bootstrap` with the capability in `bootstrap_id`
   and a fresh `operation_id`. The direct response contains the versioned
   session and render payload.
7. The app verifies the widget sources, creates the frontend model graph,
   applies launch messages, initializes each module, and renders the root
   widget.
8. When you interact with the widget, `model.save_changes()` sends pending
   browser values through the AnyWidget comm path to Python.
9. Python applies Traitlets deserialization, validation, and observers. The
   resulting updates return to the browser before the latest state projection
   is published to the host.
10. Polling starts 500 milliseconds after activity and backs off through 1, 2,
    5, 10, and 15 seconds while idle. Browser interaction resets the next poll
    to 500 milliseconds.
11. App teardown closes the browser bindings, Python widget graph, and managed
    factory.

The initial result has separate fields for separate consumers:

| Result field                                                | Consumer       | Contents                                                           |
| ----------------------------------------------------------- | -------------- | ------------------------------------------------------------------ |
| Summary `content[].text`                                    | Model and host | Widget launch message                                              |
| `urn:anywidget-mcp:bootstrap:<32-lowercase-hex-capability>` | Bundled app    | Session bootstrap capability                                       |
| `structuredContent`                                         | Model and host | Registered tool name, projected state, and `state_id` when enabled |
| `_meta.ui.resourceUri`                                      | MCP host       | App resource to render                                             |

The initial result top-level `_meta` contains a single `ui` object with
`resourceUri`. The machine marker is a separate text block whose complete value
matches the form in the table. Its capability is distinct from `instanceId`.

The app passes that capability as `bootstrap_id` to the app-only
`anywidget_bootstrap` tool and supplies an `operation_id`. The direct response
`_meta.anywidget` is the sole full runtime payload. It carries
`protocolVersion: 1`, the session and root model IDs, `sessionIdleTimeoutMs`,
loading text, models, messages, source manifest, and optional initial model
context.

The bootstrap marker and response form the adapter protocol between the Python
package and its bundled app. Application code should use AnyWidget traits,
messages, and the `anywidget-mcp` registration APIs.

The bootstrap response repeats the normalized status as
`_meta.anywidget.loadingMessage`. Hosts that mount the app after tool execution
still show the invocation-specific text while the widget sources and model
graph initialize. If a host delivers the same capability more than once, the
app resolves and applies its first delivery and ignores later deliveries.

The first bootstrap `operation_id` claims the capability. A retry with that ID
replays the same response, while another claimant is rejected. If the app never
claims a launch, Python closes it after the shorter of 30 seconds and the
configured session idle timeout.

For streamable HTTP, the Python session registry follows the Starlette
application lifespan. A host can replace its MCP connection and continue using
the same widget `instance_id`. Explicit disposal, idle expiry, `aclose()`, or
application shutdown closes the widget graph and managed factory.

## Synchronized state and model context

Trait synchronization carries browser updates to Python. Model context gives
the language model a selected projection of the current interaction.

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
the widget interactive. When projection is enabled,
`structuredContent.state` contains the initial projection. After interaction,
the app publishes complete replacement snapshots through MCP Apps
`updateModelContext` when the host supports it. See [Model-visible
state](./state) for trait selection, `StateProjection`, sequence results, and
projection limits.

The same launch result provides a `state_id` for the model-visible
`anywidget_state` tool. Hosts that omit `updateModelContext` can call this tool
when the model needs the widget's current state. The tool reads the current
Python-authoritative projection without draining comm messages, model changes,
or the projection queued for the app. Each live widget call has a distinct
state handle. Disposal and idle expiry revoke it.

## AnyWidget frontend methods

The frontend uses the AnyWidget model methods:

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

Continue with [Pass input to widgets](./factories) for runtime inputs and
managed resources, or use the [API reference](./api) for registration defaults
and errors.
