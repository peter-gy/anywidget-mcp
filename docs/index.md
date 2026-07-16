---
layout: home

hero:
  name: anywidget-mcp
  text: Bring Python widgets into the conversation.
  tagline: Register one widget or several. Browser interactions update Python traitlets.
  actions:
    - theme: brand
      text: Get started
      link: ./getting-started
    - theme: alt
      text: API reference
      link: ./api

features:
  - title: Use one widget class
    details: Run the same AnyWidget class in notebooks and MCP hosts.
  - title: Register installed widgets
    details: Register an installed widget class as an MCP App tool. anywidget-mcp provides the MCP App frontend.
  - title: Compose servers
    details: Combine widget factories, FastMCP tools, runtime inputs, and model-visible state on one server.
---

`anywidget-mcp` turns an [AnyWidget](https://anywidget.dev/) class or factory
into an interactive
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview). Each tool
call creates a fresh Python-backed widget and keeps browser changes synchronized
with traitlets.

## What the adapter handles

MCP Apps render interactive interfaces inside the conversation. Compared with
sending a standalone web app link, the UI preserves conversational context,
exchanges data through MCP, can use host-mediated capabilities, and runs in a
host-controlled sandbox.

Building an MCP App coordinates tool metadata, an HTML resource, host
communication, session state, and packaging. `anywidget-mcp` implements those
boundaries for AnyWidget. [How it works](./how-it-works) maps Python classes,
docstrings, signatures, traits, and widget sources to their MCP contracts.

Use the adapter to:

- **Author a widget.** Express application logic in Python and implement the
  interface as an AnyWidget.
- **Register a widget package.** Import a class from the
  [AnyWidget gallery](https://try.anywidget.dev/) and register its class or a
  factory around it.
- **Compose a server.** Register several widgets alongside FastMCP
  tools, with typed runtime inputs and model-visible state. See
  [Factories and composition](./factories).

## Install

Install the adapter:

```sh
pip install anywidget-mcp
```

## Try an existing widget library

[Wigglystuff](https://koaning.github.io/wigglystuff/) is an AnyWidget library.
Install it to run the `ColorPicker` example:

```sh
pip install wigglystuff
```

Expose one of its widget classes:

```sh
anywidget-mcp serve wigglystuff:ColorPicker --port 8010
```

The server listens at `http://127.0.0.1:8010/mcp`.

## See it in Inspector Chat

Keep the widget server running. Start the
[mcp-use Inspector](https://mcp-use.com/docs/inspector) in another terminal:

```sh
npx --yes @mcp-use/inspector@12.0.3 \
  --url http://127.0.0.1:8010/mcp \
  --port 7878 \
  --no-open
```

Open [Inspector Chat](http://127.0.0.1:7878/inspector?tab=chat), configure a
model provider, and ask: `Use color_picker so I can choose a color.` The widget
renders in the conversation, and later turns receive its latest synchronized
state. Keeping `--no-open` lets browser automation drive the same flow.

For runtime-input examples, [serve
`LiveEdit`](./factories#turn-an-explanation-into-an-interactive-trace) or [let
the model create an AnyWidget from
source](./factories#create-anywidgets-from-source-at-runtime).
