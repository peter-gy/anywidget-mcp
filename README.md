# anywidget-mcp

[![PyPI](https://img.shields.io/pypi/v/anywidget-mcp.svg)](https://pypi.org/project/anywidget-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://spdx.org/licenses/MIT.html)

Some answers work better as interfaces.

`anywidget-mcp` brings [AnyWidget](https://anywidget.dev/) to
[MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview). Build a
widget once, use it in Jupyter or marimo, then let people open the same
interface right inside the conversation.

Color pickers, data explorers, diagrams, and interactive explanations stay
connected to Python, so the model can respond to what the user does next.

Start with a widget library or bring your own.

**Documentation:**
[peter-gy.github.io/anywidget-mcp](https://peter-gy.github.io/anywidget-mcp/)

## Install

Requires Python 3.11 or newer.

```sh
pip install anywidget-mcp
```

## See a widget in chat

This example uses
[Wigglystuff](https://koaning.github.io/wigglystuff/), an AnyWidget library:

```sh
pip install wigglystuff
anywidget-mcp serve wigglystuff:ColorPicker --port 8010
```

Keep the server running. Start
[mcp-use Inspector](https://mcp-use.com/docs/inspector) in another terminal:

```sh
npx --yes @mcp-use/inspector@12.0.3 \
  --url http://127.0.0.1:8010/mcp \
  --port 7878 \
  --no-open
```

Open [Inspector Chat](http://127.0.0.1:7878/inspector?tab=chat), configure a
model provider, and ask:

> Use `color_picker` so I can choose a color.

The widget renders in the conversation. Change the color, then ask the model
what color it is. The answer comes from the widget's current Python state.

Browse the [AnyWidget gallery](https://try.anywidget.dev/) for other widgets.

## Serve your AnyWidget

Point the CLI at any importable AnyWidget class:

```sh
anywidget-mcp serve my_widgets:MyWidget
```

Or start the server from Python:

```python
from anywidget_mcp import serve
from my_widgets import MyWidget

serve(MyWidget)
```

`anywidget-mcp` supplies the MCP tool, app resource, session lifecycle, and
state synchronization. `MyWidget` stays an ordinary `anywidget.AnyWidget`
subclass with the same Python traits and frontend module used in notebooks.

When the widget needs data from the conversation, use a Python function that
accepts the input and returns the widget. [Runtime inputs and
composition](https://peter-gy.github.io/anywidget-mcp/factories) covers that
workflow, multiple widgets, and existing FastMCP servers.

## Documentation

- [Getting started](https://peter-gy.github.io/anywidget-mcp/getting-started)
- [Model-visible state](https://peter-gy.github.io/anywidget-mcp/state)
- [How it works](https://peter-gy.github.io/anywidget-mcp/how-it-works)
- [API reference](https://peter-gy.github.io/anywidget-mcp/api)
- [Deployment](https://peter-gy.github.io/anywidget-mcp/deployment)
