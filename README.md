<p align="center">
  <a href="https://peter-gy.github.io/anywidget-mcp/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://peter-gy.github.io/anywidget-mcp/brand/anywidget-mcp-logo-horizontal-inverse.svg">
      <img alt="anywidget-mcp" src="https://peter-gy.github.io/anywidget-mcp/brand/anywidget-mcp-logo-horizontal.svg" width="620">
    </picture>
  </a>
</p>

<p align="center">
  <a href="https://pypi.org/project/anywidget-mcp/"><img alt="PyPI" src="https://img.shields.io/pypi/v/anywidget-mcp.svg"></a>
  <a href="https://spdx.org/licenses/MIT.html"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
</p>

> **Experimental**: `anywidget-mcp` is experimental software. Its API is subject
> to breaking changes.

`anywidget-mcp` runs [AnyWidgets](https://anywidget.dev/) inside AI
conversations. You can interact with a widget directly, and the model can
respond to your input.

Start with a widget from an existing package, or bring the same widget you use in
Jupyter or marimo.

[Read the documentation](https://peter-gy.github.io/anywidget-mcp/).

## Try a multi-widget conversation

Requires Python 3.11 or newer. Serve
[Wigglystuff](https://koaning.github.io/wigglystuff/)'s `ColorPicker` and
`LiveEdit` together:

```sh
uvx --with wigglystuff anywidget-mcp serve \
  wigglystuff:ColorPicker \
  wigglystuff:LiveEdit \
  --port 8010
```

Connect a host that supports
[MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview) to
`http://127.0.0.1:8010/mcp`, then ask:

> let me pick a color

Choose a color, then ask:

> given my current selection help explain the HEX to RGB algo interactively

The model reads the picker's current color and opens `LiveEdit` for the
interactive explanation. One server exposes both widgets, so the conversation
can move from selection to explanation without leaving the chat.

<p align="center">
  <a href="https://peter-gy.github.io/anywidget-mcp/demos/anywidget-mcp-demo.mp4">
    <img alt="ColorPicker selection followed by an interactive HEX-to-RGB explanation in LiveEdit" src="https://peter-gy.github.io/anywidget-mcp/demos/anywidget-mcp-demo-poster.jpg" width="900">
  </a>
</p>
<p align="center">
  <a href="https://peter-gy.github.io/anywidget-mcp/demos/anywidget-mcp-demo.mp4">Watch the 47-second demo</a>
</p>

[Getting started](https://peter-gy.github.io/anywidget-mcp/getting-started)
walks through this flow in Inspector Chat.

## But why?

**💬 Stay in context:** A standalone web app sends you to a separate page. An
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview) stays in the
conversation, exchanges data through MCP, and runs in a host-controlled
sandbox. With your consent, it can ask the host to use tools you already
connected.

**🧩 Skip the scaffolding:** Building an MCP App directly means wiring together
tools, UI resources, browser code, the host connection, and shared state.
`anywidget-mcp` handles that integration. You define an
[AnyWidget](https://anywidget.dev/) that keeps browser behavior and Python state
in one component.

**🧪 Develop in notebooks:** The same widget runs in Jupyter and marimo. Create
it from Python, inspect its state, and use marimo's reactive execution to test
scenarios before serving it through MCP.

**🧰 Combine widgets:** One server can expose multiple widgets as separate
tools, or one call can open several together. Combine widgets from the
[AnyWidget gallery](https://try.anywidget.dev/) into an interactive environment
for the task, then add your own.

**✨ Invent at runtime:** When you cannot enumerate every useful interface in
advance, serve
[`create_anywidget`](https://peter-gy.github.io/anywidget-mcp/factories#create-anywidgets-from-source-at-runtime).
Supply Python source directly or ask an agent to invent a fresh AnyWidget during
the conversation. Use that natural-language loop to develop widgets and test
concepts. Run the factory in a sandbox because supplied source executes with the
MCP server's permissions.

## Install in a project

Install `anywidget-mcp` in the Python environment that owns your widget code:

```sh
uv pip install anywidget-mcp
```

## Bring your own AnyWidget

Point the CLI at your AnyWidget's import path:

```sh
anywidget-mcp serve my_widgets:MyWidget
```

Or start the server from Python:

```python
from anywidget_mcp import serve
from my_widgets import MyWidget

serve(MyWidget)
```

`MyWidget` remains the same `anywidget.AnyWidget` subclass you use in
notebooks. Browser changes stay synchronized with its Python traits.

When the widget needs data from the conversation, use a Python function that
accepts the input and returns an AnyWidget. This function is a widget factory.
[Pass input to widgets](https://peter-gy.github.io/anywidget-mcp/factories)
covers that workflow, multiple widgets, and existing MCP servers.

## Documentation

- [Getting started](https://peter-gy.github.io/anywidget-mcp/getting-started)
- [Model-visible state](https://peter-gy.github.io/anywidget-mcp/state)
- [Pass input to widgets](https://peter-gy.github.io/anywidget-mcp/factories)
- [How it works](https://peter-gy.github.io/anywidget-mcp/how-it-works)
- [API reference](https://peter-gy.github.io/anywidget-mcp/api)
- [Deployment](https://peter-gy.github.io/anywidget-mcp/deployment)
