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

`anywidget-mcp` runs [AnyWidgets](https://anywidget.dev/) inside AI
conversations. Interact with a Python widget, then let the model respond to its
current state. Connect through a host that supports
[MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview), the Model
Context Protocol extension for interactive interfaces.

<p align="center">
  <img alt="Choose a color, then explore an interactive HEX-to-RGB explanation" src="https://peter-gy.github.io/anywidget-mcp/demos/anywidget-mcp-demo-00.gif" width="900">
</p>

- **Bring existing widgets.** Serve widgets from a package or reuse the ones you
  develop in Jupyter and marimo.
- **Accept conversational input.** Typed Python factories become tools that
  create a fresh widget for each call.
- **Share selected state.** Python validates interactions, and you choose which
  values the model can read.
- **Explore large datasets.** Serve [query-backed viewers](https://peter-gy.github.io/anywidget-mcp/large-widgets)
  such as Embedding Atlas and share their current filter with the model.
- **Compose interfaces.** Expose several tools, return a group of widgets, or
  [create widgets from source](https://peter-gy.github.io/anywidget-mcp/generated-widgets)
  in a sandbox.

## Try it

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/), a Python package
runner:

```sh
uvx --with wigglystuff anywidget-mcp serve wigglystuff:ColorPicker --port 8010
```

Connect your MCP Apps host to `http://127.0.0.1:8010/mcp`, ask to pick a color,
and change the selection. [Getting started](https://peter-gy.github.io/anywidget-mcp/getting-started)
walks through this in Inspector Chat.

## Use Python

Install in the environment that owns your widgets:

```sh
uv pip install anywidget-mcp wigglystuff
```

```python
from anywidget_mcp import serve
from wigglystuff import ColorPicker

serve(ColorPicker, state="color")
```

`state="color"` gives the model the current color while the full widget remains
interactive. Use [`AnyWidgetMCP`](https://peter-gy.github.io/anywidget-mcp/composition)
to register several tools or `attach()` to add widgets to an existing server.

## Documentation

[Getting started](https://peter-gy.github.io/anywidget-mcp/getting-started) ·
[Write a widget](https://peter-gy.github.io/anywidget-mcp/authoring) ·
[Pass input](https://peter-gy.github.io/anywidget-mcp/factories) ·
[Share state](https://peter-gy.github.io/anywidget-mcp/state) ·
[API reference](https://peter-gy.github.io/anywidget-mcp/api) ·
[Deployment](https://peter-gy.github.io/anywidget-mcp/deployment)

Experimental software. The API may change between releases.
