# anywidget-mcp

[![PyPI](https://img.shields.io/pypi/v/anywidget-mcp.svg)](https://pypi.org/project/anywidget-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://spdx.org/licenses/MIT.html)

`anywidget-mcp` turns an [AnyWidget](https://anywidget.dev/) class or factory
into an interactive
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview). The widget
renders inside the conversation. User interactions update its Python traits,
and the model can use the current widget state in later turns.

Develop the widget once and use the same class in Jupyter, marimo, or an MCP
host. Serve an existing class, construct one from runtime inputs in a factory,
or register several widgets alongside other FastMCP tools.

[Read the documentation](https://peter-gy.github.io/anywidget-mcp/).

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

Point the CLI at any importable AnyWidget class or factory:

```sh
anywidget-mcp serve my_widgets:MyWidget
```

Or start the server from Python:

```python
from anywidget_mcp import serve
from my_widgets import MyWidget

serve(MyWidget)
```

`MyWidget` stays an ordinary `anywidget.AnyWidget` subclass and keeps the same
Python traits and frontend module used in notebook environments.

Factories can accept model-supplied inputs and construct a widget for each tool
call. `AnyWidgetMCP` can register several classes and factories on one server.

## Documentation

- [Getting started](https://peter-gy.github.io/anywidget-mcp/getting-started)
- [Factories and composition](https://peter-gy.github.io/anywidget-mcp/factories)
- [Model-visible state](https://peter-gy.github.io/anywidget-mcp/state)
- [How it works](https://peter-gy.github.io/anywidget-mcp/how-it-works)
- [API reference](https://peter-gy.github.io/anywidget-mcp/api)
- [Deployment](https://peter-gy.github.io/anywidget-mcp/deployment)
