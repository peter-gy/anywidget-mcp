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
  -> _targets.py compiles one target for registration and CLI inspection
  -> FastMCP injects Context while the factory creates or yields widget output
  -> _factory.py normalizes output and keeps its manager active
  -> _runtime.py owns the session lease, bootstrap, state handle, and cleanup
  -> _bridge.py claims widget identities and serializes models
  -> _source_assets.py externalizes ESM and CSS as content-addressed references
  -> _bridge.py snapshots the graph, messages, asset manifest, and projection
  -> the primary result links the app resource and includes a bootstrap marker
  -> app.ts accepts the host result and starts runtime replacement
  -> runtime-results.ts claims the bootstrap capability with one operation ID
  -> anywidget_bootstrap returns the versioned runtime payload
  -> assets.ts verifies source references through anywidget_assets
  -> runtime-payload.ts hydrates and validates models and messages
  -> runtime.ts creates every browser model and applies launch messages
  -> binding.ts loads ESM and CSS, then renders AnyWidget views
  -> model.ts emits canonical state and custom messages
  -> tool-calls.ts sends app-only bridge calls in order
  -> _bridge.py applies comm messages through each model's Python state handler
  -> validation and observers produce authoritative updates when provided
  -> _state.py produces one complete versioned projection
  -> context.ts publishes the projection with updateModelContext
  -> anywidget_state provides a model-visible pull path for other hosts
```

Each module owns one transition. `server.py` exposes the public FastMCP facade.
`_widget_tools.py` registers resources and tools. `_runtime.py` manages leases.
Model event behavior stays in the app, which consumes explicit protocol
payloads while Python process state stays behind the bridge tools.

## Python ownership

`WidgetTools.widget()` owns the public registration contract. `attach()` adds
that adapter to an existing `FastMCP` server. `AnyWidgetMCP` creates and
delegates to the same adapter. One compiled target derives the registration and
CLI inspection schema from a widget constructor or factory signature. FastMCP
injects `Context` parameters after schema validation. Tool annotations, icons,
the registered title, and the app resource URI are attached during the same
registration step. The `serve` command resolves every requested target before
constructing the server, then registers the targets in command-line order.

A factory result may be an AnyWidget or a non-empty `Sequence[AnyWidget]`. A
synchronous or asynchronous context manager may yield either result. Any form
may arrive through an awaitable. A sequence is snapshotted when acquired, then
`_WidgetGroup` becomes its single render root and references the returned
widgets in order. The session runtime keeps a manager active for the complete
widget session. Teardown closes the widget graph before exiting the manager.
After acquisition completes, the runtime releases its invocation argument map.
A returned manager retains the domain resources it captures for its own
cleanup.

`create_anywidget()` in `_dynamic.py` executes supplied Python source in a fresh
module. It resolves explicit `classnames` bindings after execution and
instantiates them in request order. An omitted list selects the last
final namespace binding to a source-defined top-level AnyWidget class. One
class returns a widget. Several classes return the ordered sequence handled by
the same factory runtime.

Factory acquisition runs in a cancellable scope inside a shielded owner task.
The manager exit stack lives outside that acquisition scope, so cleanup after a
successful yield can await during cancellation and shutdown. A manager that
acquires resources before a cancellable pre-yield await shields its own partial
rollback. An awaitable factory owns the same rollback until it returns.

`WidgetSession` owns every model reachable from synchronized root state. It
recursively follows references in dicts, lists, and tuples, including
descriptor-backed protocol objects. It installs one comm per model, serializes
references as `anywidget:<model_id>`, enrolls replacement children, and
collects outbound messages until the app polls. A process-wide identity claim
prevents one live widget from belonging to multiple sessions.

Detached models remain callable while the browser applies the transaction that
announced their removal. After binding disposal completes, the browser records
the removed IDs. A later poll acknowledges that set, then Python closes the
detached models before producing the poll snapshot. The poll replay identity
includes the acknowledgment set.

Before a snapshot crosses the MCP boundary, `WidgetSession` replaces `_esm`
and `_css` strings with typed SHA-256 source references. The snapshot carries a
manifest with each source kind and byte length. `anywidget_assets` returns
source text for IDs owned by that live session. The session retains the last
emitted sources for each live model and every source referenced by the latest
snapshot. Bounded comm and poll replay entries pin the sources needed to apply
their stored responses. Each ordered snapshot releases unpinned superseded
versions. Replay eviction releases versions retained by that replay.

The async `SessionRuntime` in `_runtime.py` owns factory tasks, active-call
counting, state handles, idle expiry, disposal, and shutdown. `WidgetTools` in
`_widget_tools.py` composes that runtime with the existing FastMCP lifespan.
Lifespan exit closes every session. `WidgetTools.aclose()` and
`AnyWidgetMCP.aclose()` close sessions early inside an active lifespan.

`StateContext` records the last notified value for each observed trait. Default
and selected projections serialize that committed shadow. A custom projector
runs outside the session lock only when live traits match the shadow, then
rechecks the shadow before committing. Browser comms invalidate custom
projections for protocol objects that have no observable trait API. Projectors
are read-only. `_projection_json.py` summarizes their output into bounded JSON,
then `StateContext` versions the projection for the model. For a factory
sequence, each emitted projection applies the selected state specification to
every returned widget and aggregates the results as
`{"widgets": [state, ...]}`. The internal group root stays outside that public
projection.

## Browser ownership

`app.ts` owns the Ext Apps connection, host event handlers, DOM shell, loading
state, runtime replacement, and visible errors. It installs every event handler
before connecting to the host. Runtime failures use the error reporter passed
when the app creates `WidgetRuntime`.

`WidgetRuntime` owns one bootstrapped runtime payload. It claims the bootstrap
capability with an operation ID, registers the full model graph, applies queued
launch messages, initializes bindings, renders the root view, schedules comms
and polls in API call order, applies model membership changes, and disposes the
session. It requires the matching `protocolVersion` before applying the
bootstrap payload or a transaction. Its protocol scheduler coalesces adjacent
updates for the same model while custom messages, other models, and polls remain
ordering barriers.

`runtime-payload.ts` owns payload and transaction decoding, source hydration,
model normalization, and context metadata validation. `runtime-lifecycle.ts`
owns bounded session disposal, abort propagation, and timeout helpers.

`AssetStore` validates each manifest entry and source digest. It reads verified
sources from a process-local memory cache and the browser Cache API, then asks
`anywidget_assets` for the remaining IDs. A complete transaction is hydrated
before any model update is applied, so source changes retain the same ordering
as trait updates and dynamic model enrollment. Cache reads honor the runtime
abort signal. The memory cache uses a 32 MiB least-recently-used budget measured
from UTF-8 source bytes. Cache reads and writes have a 500 ms deadline. A
fetched source enters memory before its Cache API write begins, and a write
failure does not fail asset hydration.

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
outer response finishes applying. Applied model removals are acknowledged on a
later poll. The queue releases canceled work even when a host call does not
settle, while disposal retains an immediate path.
`ModelContextSync` coalesces projections while bounding each host update. A
shared owner epoch republishes the current projection if an older host request
settles after a replacement runtime.

## Protocol boundaries

The Python and browser packages share behavior through data contracts:

- The primary tool result top-level `_meta` contains a single `ui` object with
  `resourceUri`. Its content contains the model-facing summary and a separate
  text block whose complete value is
  `urn:anywidget-mcp:bootstrap:<32-lowercase-hex-capability>`.
- Tool result `structuredContent` identifies the registered tool through the
  `tool` field. A projected result also carries a distinct `state_id`.
- `anywidget_bootstrap` accepts the capability as `bootstrap_id` and a bounded
  `operation_id`. Its response `_meta.anywidget` is the sole full runtime
  payload. The payload carries protocol version 1, session and root identity,
  `sessionIdleTimeoutMs`, model graph, ordered launch messages, source manifest,
  loading message, initial model context, and binary parts.
- `anywidget_assets` returns session-owned ESM and CSS source text for browser
  verification.
- `anywidget_comm` accepts one canonical comm message plus an idempotent
  operation ID, then returns resulting model updates and the latest projection.
- `anywidget_poll` uses one idempotent operation ID per poll cycle and returns
  queued Python-originated updates and projection changes.
- `anywidget_state` accepts the model-visible `state_id` and returns the latest
  complete projection without consuming app protocol state.
- `anywidget_dispose` releases the session and its complete widget graph.
- `ui/update-model-context` replaces the model-visible projection used by
  later chat turns.

[Widget bridge protocol](protocol.md) records the message mapping and host
behavior that motivated these contracts.

## Failure and lifecycle rules

Registration errors identify the invalid class, factory, instance, name, or
signature before the MCP server starts.

A launch failure closes the newly created widget graph and exits its factory
manager. Cancellation, app disposal, idle expiry, explicit async close, and
lifespan exit use the same cleanup path. A replay-protected bridge call retries
within its global queue slot. Exhausted delivery, an MCP tool error, or a
response-application failure aborts and disposes the runtime before later
protocol work can advance. The error remains visible in the app.

The browser claims a new bootstrap capability after joining disposal of the
prior runtime. The first operation ID owns that claim. Repeating the same ID
replays the bootstrap response, while another ID is rejected. An unclaimed
launch expires after the shorter of 30 seconds and the configured idle timeout.
The first later successful app call retires the bootstrap replay and releases
its pinned launch assets.

One abort scope spans bootstrap, cache lookup, source fetch, binding
initialization, and root render. A cancelled or superseded mount cannot proceed
to render after its initialization settles. Hot reload, experimental commands,
and resolved child renders use the runtime abort signal. Teardown cancels
protocol work, removes listeners and styles, disposes bindings, and asks Python
to close the session.

## Generated artifact

`pnpm --filter @anywidget-mcp/python build` writes
`packages/anywidget-mcp/src/anywidget_mcp/static/index.html`. Python serves
that file through `importlib.resources` at `ui://anywidget-mcp/app.html`.

The generated path stays outside source control. Distribution builds require
the file, and the package gate rebuilds it before producing the sdist and wheel.
A wheel built from the sdist must serve the same self-contained HTML resource.
