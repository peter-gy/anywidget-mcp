# Getting started

Open a color picker in an AI conversation, choose a color, then ask the model
about your selection.

You need Python 3.10+, [uv](https://docs.astral.sh/uv/getting-started/installation/)
to run the Python packages, and Node.js 22.22.2+ to run the Inspector host.
Inspector Chat also needs a model provider configured in its settings. Calls
use that provider's credentials and billing.

## Start the widget server

Serve `ColorPicker` from [Wigglystuff](https://koaning.github.io/wigglystuff/),
a collection of AnyWidgets:

```sh
uvx --from 'anywidget-mcp[server]' --with wigglystuff anywidget-mcp serve wigglystuff:ColorPicker --port 8010
```

The command keeps running and exposes the `color_picker` tool at
`http://127.0.0.1:8010/mcp`.

## See it in Inspector Chat

In another terminal, start [mcp-use Inspector](https://github.com/mcp-use/mcp-use),
a local host for [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview).
MCP Apps lets a tool render an interactive interface inside a conversation.

```sh
npx --yes @mcp-use/inspector@20.3.7 \
  --url http://127.0.0.1:8010/mcp \
  --port 7878
```

Open [Inspector Chat](http://127.0.0.1:7878/inspector?tab=chat), configure your
model provider, and ask:

> Open a color picker.

Choose a different color in the widget, then ask:

> What color did I choose? Give me its RGB values.

The answer should use your current selection. Python owns the widget state,
and the model reads it through [model-visible state](./state).

## If something fails

| Symptom                                  | Check                                                                                   |
| ---------------------------------------- | --------------------------------------------------------------------------------------- |
| Inspector cannot connect                 | Keep the Python terminal running and use its `/mcp` URL.                                |
| The port is occupied                     | Choose another `--port` and update the connecting URL. Inspector prints its actual URL. |
| A tool returns text but no widget        | Use a host with MCP Apps rendering support.                                             |
| Chat cannot call the tool                | Configure a model provider, select the connected server, and enable its tools.          |
| The model describes an earlier selection | Ask it to read the current widget state through `anywidget_state`.                      |

Stop both terminal processes with Ctrl+C when finished.

## Use your own host or widget

Connect another MCP Apps host to the same server URL. Check the
[client support list](https://modelcontextprotocol.io/extensions/apps/overview#client-support)
for host-specific setup. A host that launches Python itself can use
[standard input and output](./deployment#standard-input-and-output).

[Write a widget](./authoring) to build your own interface,
[pass input](./factories) to choose its initial values, or
[combine widgets](./composition) through one server.
