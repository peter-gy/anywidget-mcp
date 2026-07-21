---
layout: home

hero:
  name: anywidget-mcp
  text: Bring AnyWidgets into the conversation.
  tagline: Run the same Python widgets in notebooks and AI conversations. You can interact with a widget directly, and the model can respond to your input.
  image:
    light: /brand/anywidget-mcp-mark.svg
    dark: /brand/anywidget-mcp-mark-inverse.svg
    alt: anywidget-mcp
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
    link: https://anywidget.dev/?utm_source=anywidget-mcp
    linkText: Learn about AnyWidget
  - title: 🧪 Develop in notebooks
    details: >-
      Build the same widget in Jupyter or marimo, inspect its state, and test
      interactions before serving it through MCP.
    link: https://marimo.io/blog/anywidget?utm_source=anywidget-mcp
    linkText: Develop AnyWidgets in marimo
  - title: ✨ Combine or invent
    details: >-
      Expose any number of widgets as tools and open several together. Start
      with the AnyWidget gallery, or let an agent create a new interface at
      runtime with <code>create_anywidget</code>. Run generated source in a
      sandbox.
    link: https://try.anywidget.dev/?utm_source=anywidget-mcp
    linkText: Browse existing AnyWidgets
---

<script setup>
import { withBase } from 'vitepress'
</script>

`anywidget-mcp` opens an [AnyWidget](https://anywidget.dev/?utm_source=anywidget-mcp) inside an AI
conversation and keeps its Python state synchronized. Interact with the widget,
then ask the model about your input. Each widget runs as an
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview?utm_source=anywidget-mcp).

## Try a multi-widget conversation

Requires Python 3.11 or newer. Serve
[Wigglystuff](https://koaning.github.io/wigglystuff/?utm_source=anywidget-mcp)'s `ColorPicker` and
`LiveEdit` together:

```sh
uvx --with wigglystuff anywidget-mcp serve \
  wigglystuff:ColorPicker \
  wigglystuff:LiveEdit \
  --port 8010
```

Connect a host that supports MCP Apps. For a quick local setup, start the
[mcp-use Inspector](https://github.com/mcp-use/mcp-use?utm_source=anywidget-mcp) in another terminal:

```sh
npx --yes @mcp-use/inspector@12.0.3 \
  --url http://127.0.0.1:8010/mcp \
  --port 7878
```

Open [Inspector Chat](http://127.0.0.1:7878/inspector?tab=chat), configure a
model provider, then ask:

> let me pick a color

Choose a color, then ask:

> given my current selection help explain the HEX to RGB algo interactively

The model reads the picker's current color and opens `LiveEdit` for the
interactive explanation. One server exposes both widgets, so the conversation
can move from selection to explanation without leaving the chat.

<video class="demo-video" controls muted playsinline preload="metadata" :poster="withBase('/demos/anywidget-mcp-demo-poster.jpg')" aria-label="A conversation uses ColorPicker to choose a color and LiveEdit to explain HEX-to-RGB conversion" :src="withBase('/demos/anywidget-mcp-demo.mp4')"></video>

[Getting started](./getting-started) explains the Inspector flow and other MCP
clients.

## Install in a project

Install `anywidget-mcp` in the Python environment that owns your widget code:

```sh
uv pip install anywidget-mcp
```

## Next steps

- [Getting started](./getting-started) runs two widgets in Inspector Chat and then
  serves one you author.
- [Model-visible state](./state) chooses which widget values the model can use.
- [Pass input to widgets](./factories) accepts values from the model, loads
  data, and combines widgets with other tools.
- [Deployment](./deployment) runs the server for local and hosted MCP clients.
