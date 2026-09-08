---
layout: home

hero:
  name: anywidget-mcp
  text: Python widgets for AI collaboration.
  tagline: Use AnyWidgets in MCP conversations or expose live notebook widgets to browser agents.
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
---

<script setup>
import { withBase } from 'vitepress'
</script>

`anywidget-mcp` turns [AnyWidgets](https://anywidget.dev/), Python objects with
JavaScript interfaces, into tools that open inside a conversation. It uses
[MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview), the Model
Context Protocol extension for interactive tool results.

<video class="demo-video" controls muted playsinline preload="metadata" :poster="withBase('/demos/anywidget-mcp-demo-poster.jpg')" aria-label="Choose a color, then explore an interactive HEX-to-RGB explanation" :src="withBase('/demos/anywidget-mcp-demo.mp4')"></video>

## Start with a widget

```sh
uvx --from 'anywidget-mcp[server]' --with wigglystuff anywidget-mcp serve wigglystuff:ColorPicker --port 8010
```

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/), a Python package
runner. [Getting started](./getting-started) connects the server to Inspector
Chat and checks that the model reads your selection.

## Build the conversation you need

| I want to…                                         | Start here                                        |
| -------------------------------------------------- | ------------------------------------------------- |
| Render my Python widget in a conversation          | [Write a widget](./authoring)                     |
| Let a browser agent operate live notebook widgets  | [Expose widgets with WebMCP](./webmcp)            |
| Let the model provide data or starting values      | [Pass input to widgets](./factories)              |
| Choose what the model can read after interaction   | [Share state with the model](./state)             |
| Expose several tools or open widgets together      | [Combine widgets and tools](./composition)        |
| Render large datasets and query results            | [Work with large widgets](./large-widgets)        |
| Let a model create an interface from Python source | [Create widgets from source](./generated-widgets) |
| Connect a host or run a server                     | [Deployment](./deployment)                        |

[How it works](./how-it-works) explains the tool, widget, and state contracts.
The [API reference](./api) lists signatures, defaults, and errors.

Experimental software. The API may change between releases.
