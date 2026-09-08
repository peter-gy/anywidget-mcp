# Deployment

Run `anywidget-mcp` over streamable HTTP when the MCP host connects to a server
address. Use standard input and output when the host launches the server
process.

## Streamable HTTP

The CLI binds to `127.0.0.1:8000` by default:

```sh
anywidget-mcp serve my_widgets:ColorPicker
```

Set the bind address, port, and log level for a deployed process:

```sh
anywidget-mcp serve my_widgets:ColorPicker \
  --host 0.0.0.0 \
  --port 8010 \
  --log-level INFO
```

Binding to `0.0.0.0` exposes the endpoint on every attached network. Restrict
network access to the intended hosts, and place public deployments behind an
ingress that terminates HTTPS and enforces authentication.

The MCP endpoint uses the `/mcp` path. The process runs until the transport
exits or receives an interrupt.

## Standard input and output

Use `stdio` when the MCP host owns the process lifecycle:

```sh
anywidget-mcp serve my_widgets:ColorPicker --transport stdio
```

The equivalent Python call is:

```python
from anywidget_mcp.server import serve
from my_widgets import ColorPicker

serve(ColorPicker, transport="stdio")
```

## Inspect the tool contract

Inspection validates the target and prints the tool input schema without
starting a transport:

```sh
anywidget-mcp inspect my_widgets:create_explorer --json
```

Run inspection in the deployment environment so imports, evaluated type
annotations, and installed widget dependencies match the server process.

## Configure browser and app policy

`AnyWidgetMCP` accepts Python SDK
[`MCPServer`](https://github.com/modelcontextprotocol/python-sdk/tree/v2.1.1)
options plus
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview) resource
policy:

```python
from anywidget_mcp.server import AnyWidgetMCP
from my_widgets import MediaWidget

mcp = AnyWidgetMCP(
    "Media tools",
    csp={
        "connectDomains": ["https://api.example.com"],
        "resourceDomains": ["https://esm.sh"],
    },
    permissions={"camera": {}},
    cors_origins=["https://host.example.com"],
    session_idle_timeout=900,
)
mcp.widget(MediaWidget)
mcp.run(transport="streamable-http", host="127.0.0.1", port=8000)
```

Add network destinations used by widget code to the matching content security
policy field:

| Field             | Browser access                                      |
| ----------------- | --------------------------------------------------- |
| `connectDomains`  | Fetch, WebSocket, and other network connections     |
| `resourceDomains` | Scripts, styles, images, and other loaded resources |
| `frameDomains`    | Embedded frames                                     |
| `baseUriDomains`  | Document base URLs                                  |

Widgets that compile WebAssembly can request
`csp={"scriptDirectives": ["'wasm-unsafe-eval'"]}` in hosts that support the
[`scriptDirectives` extension](./api#appcsp), including mcp-use Inspector 20.3.7.
The host decides whether to grant the request. See the
[Embedding Atlas example](./large-widgets#keep-queries-with-the-data) for a widget
that also uses data URLs for its workers.

The `permissions` mapping accepts `camera`, `microphone`, `geolocation`, and
`clipboardWrite`. Request a permission when the widget uses that browser
capability.

`cors_origins` controls [cross-origin browser access (CORS)](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS)
to the streamable HTTP endpoint. Its default, `None`, allows HTTP origins with
an explicit port on `localhost`, `127.0.0.1`, and `[::1]` when the HTTP `host` is
`localhost`, `127.0.0.1`, or `::1`. This lets local browser hosts connect directly
and permits pages served by other local processes to interact with the server.
Other bind addresses require an explicit origin list. A supplied list replaces
the default. Pass `cors_origins=()` to disable cross-origin browser access.
The SDK's HTTP host and origin validation still applies.

`prefers_border` defaults to `True` and advertises the app's border preference
to the host.

## Attachment storage

Large attachments use server temporary storage. Provision that storage alongside
the memory needed by your widgets. Uploads belong to their operations, and
acknowledged responses release their retained attachments. Session cleanup
closes every remaining attachment file. See [Work with large widgets](./large-widgets)
for a query-backed viewer and session ownership.

## Session lifetime

`session_idle_timeout` defaults to `900` seconds and applies from launch onward.
Pass a positive finite number to change it, or `None` to keep sessions until
explicit disposal or server shutdown. With `None`, an abandoned app continues
to own its widget graph and storage until the server closes it.

App disposal, idle expiry, `aclose()`, and server shutdown close the complete
widget graph. A context-managed factory exits after its widget graph closes.

Inside an active server lifespan, `await mcp.aclose()` closes current sessions
and rejects further widget calls until a new lifespan starts.

## Move from the MCP 1.x integration

`anywidget-mcp` uses MCP Python SDK 2.x. Import `MCPServer` and `Context` from
`mcp.server.mcpserver` when attaching to an existing server or declaring a
request context. Pass `host`, `port`, `stateless_http`, and `json_response` to
`run()` or `streamable_http_app()` as appropriate for the transport.

The `serve()` convenience function keeps its `host` and `port` arguments.
Widget registration, factory class-name selection, and model-visible state
keep the same contracts.
