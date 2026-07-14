# Architecture

`anywidget-mcp` turns an AnyWidget class or factory into an MCP tool backed by
an interactive MCP App. The Python package owns the widget session. The private
browser package adapts that session to the AnyWidget Frontend Model API and the
MCP Apps host lifecycle.

## Workspace graph

```text
@anywidget-mcp/python
        |
        | imports
        v
@anywidget-mcp/app
```

`packages/app` is a private TypeScript package. It can be tested and packed as
one browser-runtime unit. It has no Python packaging concerns.

`packages/anywidget-mcp` is both a private TypeScript composition package and
the publishable uv workspace member. Its Vite build imports the app package and
writes one HTML resource into the Python package. Hatch then includes that
resource in the `anywidget-mcp` wheel and sdist.

The root owns orchestration and shared policy. Package manifests own runtime
dependencies and package-specific tests or builds.

## Responsibility flow

```text
tool arguments
  -> server.py registration adapter constructs a fresh AnyWidget graph
  -> _bridge.py claims widget identities and serializes models
  -> server.py snapshots the full graph, queued launch messages, and projection
  -> app.ts creates every browser model and applies launch messages
  -> binding.ts loads ESM and CSS, then renders AnyWidget views
  -> model.ts emits canonical state and custom messages
  -> tool-calls.ts sends app-visible bridge calls in order
  -> _bridge.py applies comm messages through each model's Python state handler
  -> validation and observers produce authoritative updates when provided
  -> _state.py produces one complete versioned projection
  -> context.ts publishes the projection with updateModelContext
```

Each module owns one transition. `server.py` composes the transitions and
manages leases. Model event behavior stays in the app, which consumes explicit
protocol payloads while Python process state stays behind the bridge tools.

## Python ownership

`WidgetTools.widget()` owns the public registration contract. `attach()` adds
that adapter to an existing `FastMCP` server. `AnyWidgetMCP` creates and
delegates to the same adapter. The adapter derives the tool schema from a widget
constructor or factory signature, creates a new widget for every invocation,
and opens a session after validating the returned type.

`WidgetSession` owns every model reachable from synchronized root state. It
recursively follows references in dicts, lists, and tuples, including
descriptor-backed protocol objects. It installs one comm per model, serializes
references as `anywidget:<model_id>`, enrolls replacement children, and
collects outbound messages until the app polls. A process-wide identity claim
prevents one live widget from belonging to multiple sessions.

The session lease in `WidgetTools` owns idle expiry and active-call counting.
An owned `AnyWidgetMCP.run()` closes the adapter when its runtime exits.
Embedded servers call `WidgetTools.close()` during their shutdown path.

`StateContext` records the last notified value for each observed trait. Default
and selected projections serialize that committed shadow. A custom projector
runs outside the session lock only when live traits match the shadow, then
rechecks the shadow before committing. Browser comms invalidate custom
projections for protocol objects that have no observable trait API. Projectors
are read-only. Their output is summarized into bounded JSON and versioned for
the model.

## Browser ownership

`WidgetRuntime` owns one launched tool result. It registers the full model
graph, applies queued launch messages, initializes bindings, renders the root
view, schedules comms and polls in API call order, applies model membership
changes, and disposes the session. Its protocol scheduler coalesces adjacent
updates for the same model while custom messages, other models, and polls
remain ordering barriers.

Polling starts at 500 ms after activity and backs off through 1 s, 2 s, 5 s,
10 s, and 15 s while idle. Any returned activity resets the next delay. A
browser comm interrupts the current idle wait, then schedules a poll after
500 ms.

`WidgetBinding` owns ESM loading, CSS scope, AnyWidget initialization, render
cleanup, child views, and live ESM or CSS replacement. `BridgeModel` owns the
frontend model API, local event delivery, buffered serialization, and
`save_changes()`.

`ToolCallQueue` holds each scheduled bridge transaction through transport
retries and complete response application. Dynamic model initialization
receives that active transaction so an awaited command can run before the
outer response finishes applying. The queue releases canceled work even when a
host call does not settle, while disposal retains an immediate path.
`ModelContextSync` coalesces projections while bounding each host update. A
shared owner epoch republishes the current projection if an older host request
settles after a replacement runtime.

## Protocol boundaries

The Python and browser packages share behavior through data contracts:

- Tool result `_meta.anywidget` carries session identity, root identity, the
  full model graph, ordered launch messages, ESM, CSS, and binary parts.
- Tool result `structuredContent` and model context identify the registered
  tool through the `tool` field.
- `anywidget_comm` accepts one canonical comm message plus an idempotent
  operation ID, then returns resulting model updates and the latest projection.
- `anywidget_poll` uses one idempotent operation ID per poll cycle and returns
  queued Python-originated updates and projection changes.
- `anywidget_dispose` releases the session and its complete widget graph.
- `ui/update-model-context` replaces the model-visible projection used by
  later chat turns.

[Widget bridge protocol](protocol.md) records the message mapping and host
behavior that motivated these contracts.

## Failure and lifecycle rules

Registration errors identify the invalid class, factory, instance, name, or
signature before the MCP server starts.

A launch failure closes the newly created widget graph. A replay-protected
bridge call retries within its global queue slot. Exhausted delivery, an MCP
tool error, or a response-application failure aborts and disposes the runtime
before later protocol work can advance. The error remains visible in the app.

The browser mounts a new tool result after joining disposal of the prior
runtime. Render, binding initialization, hot reload, experimental commands, and
resolved child renders use abort signals. Teardown cancels protocol work,
removes listeners and styles, disposes bindings, and asks Python to close the
session.

## Generated artifact

`pnpm --filter @anywidget-mcp/python build` writes
`packages/anywidget-mcp/src/anywidget_mcp/static/index.html`. Python serves
that file through `importlib.resources` at `ui://anywidget-mcp/widget.html`.

The generated path stays outside source control. Distribution builds require
the file, and the package gate rebuilds it before producing the sdist and wheel.
A wheel built from the sdist must serve the same self-contained HTML resource.
