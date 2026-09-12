# Pass input to widgets

Use a typed Python function when the model should provide values before an
[AnyWidget](https://anywidget.dev/) opens. Its parameters define the tool input.
The server validates that input before it calls the function to create the
widget. This function is a widget factory.

The examples use widgets from
[Wigglystuff](https://koaning.github.io/wigglystuff/). You can use AnyWidgets
from any package, including widgets you author.

## Let the model choose the starting value

Register the function with `@mcp.widget`:

```python
from anywidget_mcp.server import AnyWidgetMCP
from wigglystuff import ColorPicker

mcp = AnyWidgetMCP("Color tools")


@mcp.widget(state="color")
def pick_color(color: str = "#315efb") -> ColorPicker:
    """Open a color picker at the requested hexadecimal value."""
    return ColorPicker(color=color)


mcp.run(transport="streamable-http")
```

When the model calls `pick_color`, the requested color becomes the picker's
starting value. You can change it in the conversation, then ask the model which
color you chose. `state="color"` exposes the picker's current color.

The explicit `color` parameter also defines the widget argument in the JSON
input schema. `anywidget-mcp` adds the optional
`loading_message` field, which a model can set to progress text shown in the
host while the widget initializes. The docstring becomes the tool description.

Use explicit named parameters for factory input. Positional parameters that
accept keyword arguments are supported. Positional-only, `*args`, and
`**kwargs` factory parameters raise `TypeError` during registration because
they cannot define object properties in the tool schema.

## Choose between a widget and a factory

Register an AnyWidget class directly when its constructor already matches the
input the model should provide. Use a widget factory to rename or validate
inputs, load data, configure the widget, or hold a resource while the widget is
active.

## Use the request context

Add each registration before the server's `mcp.run()` call.

A Python SDK
[`Context`](https://github.com/modelcontextprotocol/python-sdk/tree/v2.1.1)
parameter receives the active request context and stays
outside the generated tool input schema:

```python
from mcp.server.mcpserver import Context


@mcp.widget
async def prepared_picker(
    color: str = "#315efb",
    *,
    ctx: Context,
) -> ColorPicker:
    await ctx.info(f"Opening {color}")
    return ColorPicker(color=color)
```

Synchronous and asynchronous factories may return the widget directly.
Synchronous calls run in worker threads so other widget sessions remain
responsive during setup. Use an async factory for setup that needs the server's
event loop.

## Hold resources for the session

A factory may return a synchronous or asynchronous context manager that yields
an AnyWidget or a non-empty sequence of AnyWidgets. The manager stays active
while the app session uses the returned widget graph. This integration
excerpt assumes your `DatasetExplorer` widget and asynchronous `open_dataset`
function. `open_dataset` returns an object with an `aclose()` method:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from mcp.server.mcpserver import Context

from my_widgets import DatasetExplorer, open_dataset


@mcp.widget
@asynccontextmanager
async def explore_dataset(
    rows: list[str],
    ctx: Context,
) -> AsyncIterator[DatasetExplorer]:
    await ctx.report_progress(0, 2, "Preparing dataset")
    dataset = await open_dataset(rows)
    try:
        await ctx.report_progress(1, 2, "Opening explorer")
        yield DatasetExplorer(dataset=dataset)
    finally:
        with anyio.CancelScope(shield=True):
            await dataset.aclose()
```

App disposal, idle expiry, `aclose()`, and server shutdown close the widget
graph before the manager exits. Synchronous manager entry and exit also run in
worker threads. Synchronous invocation, entry, and exit share one copied
[context](https://docs.python.org/3/library/contextvars.html), while their thread
identities may differ. Async factories, returned awaitables, and async managers
use the owner task's context. Changes made to context variables in a worker stay
in the copied context. Put setup that must affect async task-local context in
an async factory or manager. Async managers enter and exit on the same owner task.

A cancellation delivered to the server interrupts async acquisition. A synchronous operation already
running finishes before its returned widget graph and manager are cleaned up.
A manager that acquires a resource before its final pre-yield await must protect
cleanup for that partial acquisition with `anyio.CancelScope(shield=True)`.

See [Model-visible state](./state) for projection choices and [API
reference](./api) for registration options.

## Reopen saved results

Set `reopen="auto"` to recreate a widget when a saved result can no longer
bootstrap:

```python
@mcp.widget(reopen="auto", state="color")
def pick_color(color: str = "#315efb") -> ColorPicker:
    """Open a color picker at the requested color."""
    return ColorPicker(color=color)
```

The app calls `pick_color` through the host with the saved, validated inputs,
including defaults. It uses the normal loading status and renders a fresh widget
at its original starting color. Automatic mode adds no recovery controls by default.

Use `reopen="manual"` to offer a **Reopen** button instead. Set `reopen_ui=True`
to also offer that button in automatic mode, or `reopen_ui=False` to suppress the
built-in controls. The default `reopen_ui=None` selects controls for manual mode
and omits them for automatic mode. Widget code needs no MCP-specific UI or callbacks.

Automatic recovery runs once while mounting a result. An active-session failure
requires a reload or the opt-in controls. Without controls, automatic mode reports
failures through the normal error status and explains when to reload the result.
The default `reopen=None` keeps recreation disabled.

Each reopening has fresh widget and state IDs, current request context, and the
usual session cleanup. Managed factories acquire their resources again.
Construction, observers, and frontend initialization run again, so enable this
option for factories whose setup is safe to repeat. Previous interactions and
commands are not replayed. A failed creation request is not automatically retried;
if its response was lost, another click may create another session.

Reopening stores up to 64 KiB of creation metadata in the original tool result.
Inputs must have a faithful Pydantic JSON round trip and contain finite,
non-secret values. Inputs that cannot be serialized fail before the factory runs.
Injected context and dependencies are resolved for the new request. Keep durable
identifiers in the inputs and acquire temporary resources inside the factory.
For large or sensitive requests, accept an identifier for data in your own storage.

The host must preserve the original result metadata and permit app-originated
tool calls. Results created before reopening was enabled cannot acquire this
metadata retroactively. Saved inputs pass through current validation and access
checks; they are not authorization tokens or snapshots of the old factory code.
An incompatible deployment can reject them. The app publishes the new state and
`state_id` through `updateModelContext`. Hosts without that capability must expose
the new tool result to the model for later `anywidget_state` reads.

### When reopening fails

A rejected creation request keeps the tool's error and identifies the attempted
tool. Reload the result, use **Reopen** when enabled, or request a new widget with
updated inputs. A lost response reports an unconfirmed outcome because the factory
may already have run. Another click can create another session.

If construction succeeds but rendering fails, the new session is disposed before
the optional **Reopen** control becomes available again. A terminal protocol or
connection failure stops the active graph and reports how to reload or reopen it. Unsupported or
missing creation metadata requires a new widget tool call.

Cancellation depends on the host and transport. With the SDK's stateless HTTP
transport, separate requests do not share a cancellation registry. Closing the
app can leave acquisition running until it completes or the server shuts down.
Use timeouts around external I/O in factories. Once construction completes, an
unclaimed session follows the configured idle lifetime. With an idle lifetime of
`None`, explicit disposal or server shutdown owns that cleanup.

For expected application failures, raise the SDK's `ToolError` with the affected
resource and a next action. Unexpected exceptions remain in server logs. Reopening
input errors name the tool and argument without including its value. Secret values
are rejected before custom serializers run.
