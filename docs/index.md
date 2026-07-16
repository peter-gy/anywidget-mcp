---
layout: home

hero:
  name: anywidget-mcp
  text: Bring Python widgets into the conversation.
  tagline: Register one widget or compose many. Keep browser interaction synchronized with Python.
  actions:
    - theme: brand
      text: Get started
      link: ./getting-started
    - theme: alt
      text: API reference
      link: ./api

features:
  - title: Build once
    details: Develop the interface as a portable Python widget, then use it in notebooks and MCP hosts.
  - title: Reuse the ecosystem
    details: Turn an installed widget class into an interactive tool without creating a separate frontend project.
  - title: Compose servers
    details: Combine widget factories, ordinary MCP tools, runtime inputs, and model-visible state on one server.
---

`anywidget-mcp` turns an [AnyWidget](https://anywidget.dev/) class or factory
into an interactive
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview). Each tool
call creates a fresh Python-backed widget and keeps browser changes synchronized
with traitlets.

## Why build this way

MCP Apps render interactive interfaces inside the conversation. Compared with
sending a standalone web app link, the UI preserves conversational context,
exchanges data through MCP, can use host-mediated capabilities, and runs in a
host-controlled sandbox.

A direct MCP App implementation coordinates tool metadata, an HTML resource,
host communication, session state, and packaging. `anywidget-mcp` owns those
boundaries for AnyWidget, which gives you three paths:

- **Build from scratch.** Express application logic in Python and develop the
  bespoke interface as a reusable AnyWidget.
- **Reuse any existing AnyWidget.** Import a package from the
  [AnyWidget gallery](https://try.anywidget.dev/) and register its class or a
  factory around it.
- **Compose a custom server.** Register several widgets alongside ordinary MCP
  tools, with typed runtime inputs and model-visible state. See
  [Factories and composition](./factories).

## Install

Install the adapter:

```sh
pip install anywidget-mcp
```

## Try an existing widget library

[Wigglystuff](https://koaning.github.io/wigglystuff/) is a rich collection of
expressive AnyWidgets. Install it as one concrete example:

```sh
pip install wigglystuff
```

Expose one of its widget classes:

```sh
anywidget-mcp serve wigglystuff:ColorPicker
```

The server listens at `http://127.0.0.1:8000/mcp`. An MCP Apps-compatible
host can invoke the generated tool and render the widget.
