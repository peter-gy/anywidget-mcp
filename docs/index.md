---
layout: home

hero:
  name: anywidget-mcp
  text: Bring AnyWidgets into the conversation.
  tagline: Run the same Python widgets in notebooks and AI conversations. You can interact with a widget directly, and the model can respond to your input.
  actions:
    - theme: brand
      text: Get started
      link: ./getting-started
    - theme: alt
      text: API reference
      link: ./api

features:
  - title: Open existing widgets
    details: Launch a ready-made AnyWidget with one command.
  - title: Bring your own widget
    details: Reuse the same AnyWidget in Jupyter, marimo, and AI conversations.
  - title: Ask about the widget
    details: The model can respond to your input.
---

`anywidget-mcp` opens an [AnyWidget](https://anywidget.dev/) inside an AI
conversation and keeps its Python state synchronized. Interact with the widget,
then ask the model about your input. Each widget runs as an
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview).

## Try an AnyWidget

Requires Python 3.11 or newer. Start
[Wigglystuff](https://koaning.github.io/wigglystuff/)'s `ColorPicker` with one
command:

```sh
uvx --with wigglystuff anywidget-mcp serve wigglystuff:ColorPicker --port 8010
```

Connect a host that supports MCP Apps to `http://127.0.0.1:8010/mcp` and open
`color_picker`. Choose a color, then ask the model which color you chose. The
model reads the picker's current color when it answers.

[Getting started](./getting-started) walks through the complete Inspector Chat
flow.

## But why?

A standalone web app sends you to a separate page. An
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview) stays in the
conversation, exchanges data through MCP, and runs in a host-controlled
sandbox. With your consent, it can ask the host to use tools you already
connected.

Building an MCP App directly means wiring together tools, UI resources, browser
code, the host connection, and shared state. `anywidget-mcp` handles that
integration. You define an [AnyWidget](https://anywidget.dev/) that keeps
browser behavior and Python state in one component.

The same widget runs in Jupyter and marimo. Create it from Python, inspect its
state, and use marimo's reactive execution to test scenarios before serving it
through MCP.

One server can expose multiple widgets as separate tools, or one call can open
several together. Combine widgets from the
[AnyWidget gallery](https://try.anywidget.dev/) into an interactive environment
for the task, then add your own.

When you cannot enumerate every useful interface in advance, serve
[`create_anywidget`](./factories#create-anywidgets-from-source-at-runtime). You
can supply Python source directly or ask an agent to invent a fresh AnyWidget
during the conversation. Use that natural-language loop to develop widgets and
test concepts. Run the factory in a sandbox because supplied source executes
with the MCP server's permissions.

## Install in a project

Install `anywidget-mcp` in the Python environment that owns your widget code:

```sh
uv pip install anywidget-mcp
```

## Next steps

- [Getting started](./getting-started) runs a widget in Inspector Chat and then
  serves one you author.
- [Model-visible state](./state) chooses which widget values the model can use.
- [Pass input to widgets](./factories) accepts values from the model, loads
  data, and combines widgets with other tools.
- [Deployment](./deployment) runs the server for local and hosted MCP clients.
