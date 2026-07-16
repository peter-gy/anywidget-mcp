# API reference

The Python package exports registration, composition, state projection, and MCP
App resource types from `anywidget_mcp`.

## `serve()`

```text
serve(
    target,
    *,
    name=None,
    title=None,
    description=None,
    state=<default>,
    annotations=None,
    icons=None,
    transport="streamable-http",
    host="127.0.0.1",
    port=8000,
    log_level="INFO",
    **fastmcp_options,
) -> None
```

Registers one AnyWidget class or factory and runs its MCP server until the
transport exits.

- `target` accepts an `AnyWidget` subclass or a factory that creates a fresh
  widget.
- `name`, `title`, and `description` define the host-facing tool identity.
- `state` accepts the forms documented in [Model-visible state](./state).
- `annotations` and `icons` accept standard MCP tool metadata values.
- `transport` accepts `"streamable-http"` or `"stdio"`.
- `host`, `port`, and `log_level` configure streamable HTTP.
- `fastmcp_options` are forwarded to `AnyWidgetMCP`.

Registration errors propagate before the transport starts. Widget sessions
close when the transport exits or raises.

## `AnyWidgetMCP`

```python
AnyWidgetMCP(
    name=None,
    *,
    app_uri=APP_RESOURCE_URI,
    csp=None,
    permissions=None,
    prefers_border=True,
    cors_origins=(),
    session_idle_timeout=900.0,
    **fastmcp_options,
)
```

Extends `mcp.server.fastmcp.FastMCP` with an MCP App resource, widget tools, and
session ownership.

- `app_uri` identifies the HTML resource attached to registered widget tools.
- `csp` accepts an `AppCSP` mapping.
- `permissions` accepts an `AppPermissions` mapping.
- `prefers_border` sets the app resource's host border preference.
- `cors_origins` lists browser origins allowed to call the HTTP endpoint.
- `session_idle_timeout` sets the idle lifetime in seconds. It must be positive
  and finite.
- `fastmcp_options` are forwarded to `FastMCP`.

### `AnyWidgetMCP.widget()`

```text
mcp.widget(
    target=None,
    /,
    *,
    name=None,
    title=None,
    description=None,
    state=<default>,
    annotations=None,
    icons=None,
)
```

Registers an AnyWidget class or factory as an MCP App tool. Pass `target`
directly or omit it to use `widget()` as a decorator.

The method returns the registered target unchanged. The target signature
defines the MCP input schema after FastMCP removes its injected `Context`
parameter. A factory may return an AnyWidget or a synchronous or asynchronous
context manager that yields one. An async factory may resolve to any of those
results.

Registration raises `TypeError` for an invalid target, signature, or `state`
option. It raises `ValueError` for a reserved or duplicate tool name. Invocation
returns a tool error when the target cannot create or open an AnyWidget.

### `AnyWidgetMCP.aclose()`

```python
await mcp.aclose()
```

Closes every live widget session in the active server lifespan.

## `attach()`

```python
attach(
    mcp,
    *,
    app_uri=APP_RESOURCE_URI,
    csp=None,
    permissions=None,
    prefers_border=True,
    session_idle_timeout=900.0,
) -> WidgetTools
```

Adds the MCP App resource, widget registration, session tools, and cleanup to an
existing `FastMCP` server. The returned `WidgetTools` provides `widget()` with
the same registration contract as `AnyWidgetMCP.widget()`.

`attach()` raises `TypeError` when `mcp` is not a `FastMCP` server. It raises
`ValueError` when widget tools are already attached, a reserved tool name is in
use, or `app_uri` is invalid.

### `WidgetTools.aclose()`

```python
await widgets.aclose()
```

Closes every live widget session in the active server lifespan.

## `StateProjection`

```python
StateProjection(project, *, watch=None)
```

Defines a read-only model-visible mapping and its invalidation source.

- `project` receives the root widget and returns a mapping.
- `watch=None` observes traits across the enrolled widget graph.
- `watch="value"` observes one root trait.
- `watch=("value", "selection")` observes selected root traits.
- `watch=()` computes the mapping once when the session opens.

Construction raises `TypeError` when `project` is not callable or `watch` does
not contain trait names. Opening a widget session raises `ValueError` when a
named root trait is absent.

## App resource types

### `AppCSP`

`AppCSP` is a typed mapping with optional `connectDomains`, `resourceDomains`,
`frameDomains`, and `baseUriDomains` lists.

### `AppPermissions`

`AppPermissions` is a typed mapping with optional `camera`, `microphone`,
`geolocation`, and `clipboardWrite` entries.

### Resource constants

| Name               | Value                              |
| ------------------ | ---------------------------------- |
| `APP_RESOURCE_URI` | `"ui://anywidget-mcp/widget.html"` |
| `APP_MIME_TYPE`    | `"text/html;profile=mcp-app"`      |
