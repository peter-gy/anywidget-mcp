---
layout: home

hero:
  name: anywidget-mcp
  text: Turn an AnyWidget into an MCP App.
  tagline: Register an AnyWidget class or factory. Each tool call creates a fresh Python-backed widget and keeps browser changes synchronized with traitlets.
  actions:
    - theme: brand
      text: Get started
      link: ./getting-started
    - theme: alt
      text: API reference
      link: ./api

features:
  - title: Existing AnyWidgets
    details: Serve an installed AnyWidget class from the command line or register the same class in Python.
  - title: Runtime inputs
    details: Use a typed factory when an MCP tool call needs to validate input, load data, or hold a resource for the widget session.
  - title: Model-visible state
    details: Select synchronized traits or define a read-only projection that the host can add to later model context.
---

Install the adapter and a widget package:

```sh
pip install anywidget-mcp wigglystuff
```

Expose an installed widget class:

```sh
anywidget-mcp serve wigglystuff:ColorPicker
```

The server listens at `http://127.0.0.1:8000/mcp`. An MCP Apps-compatible
host can invoke the generated tool and render the widget.
