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
  - title: 🧩 Skip the scaffolding
    details: >-
      Turn an AnyWidget into an MCP App without wiring tools, UI resources,
      browser code, and shared state yourself. Keep browser behavior and Python
      state in one component.
    link: https://anywidget.dev/
    linkText: Learn about AnyWidget
  - title: 🧪 Develop in notebooks
    details: >-
      Build the same widget in Jupyter or marimo, inspect its state, and test
      interactions before serving it through MCP.
    link: https://marimo.io/blog/anywidget
    linkText: Develop AnyWidgets in marimo
  - title: ✨ Combine or invent
    details: >-
      Expose any number of widgets as tools and open several together. Start
      with the AnyWidget gallery, or let an agent create a new interface at
      runtime with <code>create_anywidget</code>. Run generated source in a
      sandbox.
    link: https://try.anywidget.dev/
    linkText: Browse existing AnyWidgets
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
