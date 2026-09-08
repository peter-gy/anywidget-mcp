# API reference

The Python package exports registration, composition, state projection, and
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview) resource
types from `anywidget_mcp`.

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
    **mcp_options,
) -> None
```

Registers one [AnyWidget](https://anywidget.dev/) class or factory and runs its
MCP server until the transport exits.

- `target` accepts an `AnyWidget` subclass or a factory that returns an
  AnyWidget or non-empty `Sequence[AnyWidget]`.
- `name`, `title`, and `description` define the host-facing tool identity.
- `state` accepts the forms documented in [Model-visible state](./state).
- `annotations` and `icons` accept MCP tool metadata values.
- `transport` accepts `"streamable-http"` or `"stdio"`.
- `host`, `port`, and `log_level` configure streamable HTTP.
- `mcp_options` are forwarded to `AnyWidgetMCP`.

Registration errors propagate before the transport starts. Widget sessions
close when the transport exits or raises.

## `AnyWidgetMCP`

```text
AnyWidgetMCP(
    name=None,
    *,
    app_uri=APP_RESOURCE_URI,
    csp=None,
    permissions=None,
    prefers_border=True,
    cors_origins=None,
    session_idle_timeout=900.0,
    **mcp_options,
)
```

Extends the Python SDK
[`MCPServer`](https://github.com/modelcontextprotocol/python-sdk/tree/v2.1.1) with an MCP App resource, widget tools, and
session ownership.

- `app_uri` identifies the HTML resource attached to registered widget tools.
- `csp` accepts an `AppCSP` mapping.
- `permissions` accepts an `AppPermissions` mapping.
- `prefers_border` sets the app resource's host border preference.
- `cors_origins` lists browser origins allowed to call the HTTP endpoint.
  The default `None` allows HTTP origins with an explicit port on `localhost`,
  `127.0.0.1`, and `[::1]` when the HTTP `host` is a loopback address.
  A supplied list replaces that default. Pass `()` to disable cross-origin access.
- `session_idle_timeout` sets the idle lifetime from launch onward, in seconds.
  It defaults to `900.0` and accepts a positive finite number or `None`. With
  `None`, explicit disposal or server shutdown owns session cleanup.
- `mcp_options` are forwarded to `MCPServer`.

The streamable HTTP app owns widget sessions for its Starlette application
lifespan. MCP connection rotation keeps existing widget sessions addressable
through their `instance_id`. Explicit disposal, idle expiry, `aclose()`, and
application shutdown close those sessions.

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

The method returns the registered target unchanged. Explicit target parameters
define widget fields in the MCP input schema. MCPServer removes its injected
`Context` parameter from that schema. Registration adds an optional
`loading_message` string when the target has no parameter with that name. It defaults to
`"Initializing {tool title}…"` when that text passes the status bounds, with
`"Initializing widget…"` as the fallback. A model can set it to describe the
current invocation while the app initializes. The adapter normalizes the value
to one line and uses the fallback for empty values, values longer than 120
characters, control characters, and directional control characters. It removes
the generated argument before calling the target.

A target that declares `loading_message` keeps its annotation, default, schema,
and Python argument. A valid string value also supplies the app status. A value
that cannot be used as status text selects the generated status default while
the target receives its validated argument.

A factory may return an AnyWidget or a non-empty `Sequence[AnyWidget]`. It may
also return a synchronous or asynchronous context manager that yields either
result. An async factory may resolve to any of these forms.

A sequence renders its widgets in order through one MCP App result. The same
`state` specification applies to each widget. With state projection enabled,
the aggregate projection has a `widgets` field. It contains an ordered state
list within the projection byte budget, including for a one-item sequence.
An oversized list becomes a sequence summary. The summary sets
`type` to `"sequence"` and includes `length`. It may also include bounded
`items`, an `omitted` count, and `jsonBytes`. Set `state=None` to disable the
aggregate projection.

Registration raises `TypeError` for an invalid target, signature, or `state`
option. It raises `ValueError` for a reserved or duplicate tool name. Invocation
returns a tool error when the target cannot create or open its widget result.

### `AnyWidgetMCP.run()` and `streamable_http_app()`

```python
mcp.run(transport="streamable-http", host="127.0.0.1", port=8010)
```

`run()` blocks until the transport exits. It defaults to `"stdio"`, following
`MCPServer`. Pass `"streamable-http"` for an HTTP endpoint. HTTP settings belong
to the transport call:

```python
app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)
```

`streamable_http_app()` returns a Starlette application for an ASGI server.
Its application lifespan owns widget sessions. Configure `cors_origins` on
`AnyWidgetMCP` before creating the app. See [Deployment](./deployment) for
transport and access policy.

### `AnyWidgetMCP.aclose()`

```python
await mcp.aclose()
```

Closes every live widget session and rejects new widget calls until a new
server lifespan starts.

## `attach()`

```text
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
existing `MCPServer` server. The returned `WidgetTools` provides `widget()` with
the same registration contract and streamable HTTP session ownership as
`AnyWidgetMCP.widget()`. `session_idle_timeout` has the same contract as
`AnyWidgetMCP`. Call `attach()` before creating the server's
`streamable_http_app()` so the application lifespan owns widget sessions.

`attach()` raises `TypeError` when `mcp` is not a `MCPServer` server. It raises
`ValueError` when widget tools are already attached, a reserved tool name is in
use, `app_uri` is invalid, or the streamable HTTP app already exists.

### `WidgetTools.aclose()`

```python
await widgets.aclose()
```

Closes every live widget session and rejects new widget calls until a new
server lifespan starts.

## `create_anywidget()`

```text
create_anywidget(
    code: str,
    *,
    classnames: Sequence[str] = (),
) -> AnyWidget | Sequence[AnyWidget]
```

Executes `code`, resolves each `classnames` entry in the final module namespace,
and constructs the selected classes in list order. Every name must exist and
bind an AnyWidget subclass with a zero-argument constructor. An omitted or
empty `classnames` value selects the last final namespace binding to a
source-defined top-level AnyWidget class.

One selected class returns an AnyWidget. Several selected classes return a
sequence in list order. Register the function directly or serve its import
target:

```sh
anywidget-mcp serve anywidget_mcp:create_anywidget
```

`code` executes with the MCP server process permissions, and each widget's
JavaScript loads in the app iframe. Run this factory in a sandbox with scoped
filesystem, network, credential, and process access.

Compilation, class selection, execution, and construction failures raise the
Python SDK's `ToolError`. Its message includes the original exception type,
the generated source line when available, and up to 1,000 characters of error
detail. MCP clients receive the same diagnostic, such as
`TypeError at line 6: Child.__init__() missing 1 required positional argument: 'value'`.
Correct the source or `classnames` and call the tool again. Construction failures
close previously constructed widget graphs before reporting the error.

## `StateProjection`

```text
StateProjection(project, *, watch=None, max_bytes=8000)
```

Defines a read-only model-visible mapping and its invalidation source.

- `project` receives the root widget and returns a mapping. For a sequence
  result, it receives each returned widget in order.
- `watch=None` observes traits across the enrolled widget graph.
- `watch="value"` observes one root trait.
- `watch=("value", "selection")` observes selected root traits.
- `watch=()` computes the mapping once when the session opens.
- `max_bytes` budgets the complete projection as compact UTF-8 JSON, including
  every widget in a sequence. It accepts an integer of at least `2` or `None`.
  Values that fit retain their full shape. Oversized values become summaries.
  `None` preserves trusted finite input subject to JSON conversion and Python
  recursion limits. Match the budget to the host and model context capacity.

Construction raises `TypeError` when `project` is not callable or `watch` does
not contain trait names. An invalid `max_bytes` raises `ValueError`. Opening a
widget session raises `ValueError` when a named root trait is absent. For a sequence result, `watch` and named state traits
must exist on every returned widget.

## App resource types

### `AppCSP`

`AppCSP` is a typed mapping with optional `connectDomains`, `resourceDomains`,
`frameDomains`, and `baseUriDomains` lists.

`scriptDirectives` optionally requests `"'wasm-unsafe-eval'"` for WebAssembly
compilation or `"'unsafe-eval'"` for JavaScript string evaluation. It accepts a
list containing those exact strings. Other entries raise `ValueError`, and a
non-list value raises `TypeError` when constructing the server or attaching
widget tools. The default app policy requests neither directive.

This field is a host extension supported by
[mcp-use Inspector](https://github.com/mcp-use/mcp-use/tree/main/libraries/typescript/packages/inspector)
20.3.7. Check the host's support before relying on it. The host controls the final
[content security policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/script-src).

### `AppPermissions`

`AppPermissions` is a typed mapping with optional `camera`, `microphone`,
`geolocation`, and `clipboardWrite` entries.

### Resource constants

| Name               | Value                           |
| ------------------ | ------------------------------- |
| `APP_RESOURCE_URI` | `"ui://anywidget-mcp/app.html"` |
| `APP_MIME_TYPE`    | `"text/html;profile=mcp-app"`   |
