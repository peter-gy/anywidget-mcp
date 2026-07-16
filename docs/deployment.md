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
from anywidget_mcp import serve
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

`AnyWidgetMCP` accepts FastMCP server options plus
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview) resource
policy:

```python
from anywidget_mcp import AnyWidgetMCP
from my_widgets import MediaWidget

mcp = AnyWidgetMCP(
    "Media tools",
    host="127.0.0.1",
    port=8000,
    csp={
        "connectDomains": ["https://api.example.com"],
        "resourceDomains": ["https://esm.sh"],
    },
    permissions={"camera": {}},
    cors_origins=["https://host.example.com"],
    session_idle_timeout=900,
)
mcp.widget(MediaWidget)
mcp.run(transport="streamable-http")
```

Add network destinations used by widget code to the matching content security
policy field:

| Field             | Browser access                                      |
| ----------------- | --------------------------------------------------- |
| `connectDomains`  | Fetch, WebSocket, and other network connections     |
| `resourceDomains` | Scripts, styles, images, and other loaded resources |
| `frameDomains`    | Embedded frames                                     |
| `baseUriDomains`  | Document base URLs                                  |

The `permissions` mapping accepts `camera`, `microphone`, `geolocation`, and
`clipboardWrite`. Request a permission when the widget uses that browser
capability.

`cors_origins` controls browser origins that may call the streamable HTTP
endpoint. `prefers_border` defaults to `True` and advertises the app's border
preference to the host.

## Session lifetime

`session_idle_timeout` is a positive number of seconds and defaults to `900`.
App disposal, idle expiry, `aclose()`, and server shutdown close the complete
widget graph. A context-managed factory exits after its widget graph closes.

Inside an active server lifespan, `await mcp.aclose()` closes current sessions
and rejects further widget calls until a new lifespan starts.
