# Architecture

`anywidget-mcp` turns an AnyWidget class or factory into an MCP tool backed by
an interactive app. Python owns widget state, graph identity, and session
resources. The browser owns rendering and host integration. A versioned
protocol carries ordered operations and immutable attachments between them.

The base package also instruments widgets owned by notebook hosts through
`webmcp.enable()`. The `server` extra adds MCP registration, transport, and
session ownership through `anywidget_mcp.server`.

## Shared widget specifications

`_spec/` owns target classification, identity, resolved parameters, result hints,
trait inspection, schemas, and capability mappings. MCP Apps and WebMCP consume
these records through argument compiler ports. Live models implement the
`ModelBinding` and `CommPort` contracts declared inward in `_spec/ports.py`.

[Widget specifications](specifications.md) explains the intermediate
representation, policy boundaries, extension points, and responsibility map.

## WebMCP instrumentation

`webmcp.enable(widgets=..., discover=...)` configures one active displayable
`Session` per notebook runtime. `_webmcp/session.py` owns registrations, discovery policy, creation
requests, and the widgets created by those requests. Repeated calls patch target
settings. `disable()` withdraws exposure, while `Session.close()` also closes
its created widgets. Explicitly supplied instances retain their existing owners.

`_webmcp/policy.py` resolves class, originating-function, and instance settings.
It selects from shared `ModelSpec` records and snapshots configuration values.
The shared JSON input compiler validates creation arguments. Instrumentation
applies the selected capabilities to every descriptor, read, and update.

`_webmcp/hooks.py` installs one ipywidgets construction dispatcher and routes
new widgets to their runtime's session before the host captures initial state.
Invocation ownership is task-local and ends when the creation call settles.
`_webmcp/instrument.py` scans widgets owned by that runtime and applies the
session policy to existing and future instances. It clones each selected AnyWidget's `_esm` trait and wraps its
`to_json` serializer. Python source values and host comms remain with their
existing owners.

`_webmcp/host.py` captures a host with synchronous execution scope and disposal
binding. The ipywidgets host binds session disposal to comm closure. The marimo host
identifies its runtime context so independent notebook clients have independent
sessions and discovery. It also binds it to the originating cell and supplies that cell's context
while constructing widgets or publishing source. The shared engine owns
registration, exposure policy, creation, replay, and cleanup. Host disposal
restores Python serializers and withdraws metadata without publishing new source
files into a cell being disposed.

The synchronized `_webmcp` metadata carries instance identity and tool schemas.
Identical widget sources retain identical serialized `_esm` bytes across
instances, so the MCP App can reuse its verified source cache. The session also
synchronizes its creation catalog, exposed model references, and created widgets.

`packages/app/src/_webmcp/` wraps the original widget definition.
`_webmcp/instance.ts` publishes its state tools, and `_webmcp/registry.ts` shares
registrations across views in the same document. `_webmcp/session.ts` publishes
creation tools and resolves existing models through the native host. Created
widgets render through `host.getWidget(ref).render(...)`. Source loading is
shared with the MCP App through `widget-definition.ts` and `module-loader.ts`.
The standalone `static/webmcp.js` asset is composed separately from
`static/index.html`.

WebMCP requests use native AnyWidget custom messages with the
`anywidget-webmcp` kind and a request ID. Python reads state, applies an update,
or validates creation arguments and invokes their target. It publishes canonical
trait values and sends `anywidget-webmcp-result` with the same ID. State is
bounded by the shared projection serializer. Creation replies also identify the
widget reference and its read and update tools. The browser waits for the new
widget's rendering and registrations before resolving a creation call.

The session retains completed creation replies until it closes and tracks pending
requests by ID and argument fingerprint. Repeated requests replay the response.
Reusing an ID with different arguments fails. A new browser tool invocation has a new request ID
and creates a new widget.

View cleanup releases browser registrations. Disabling instrumentation restores
serializers and detaches message callbacks while leaving widgets open. The
adapter uses the host's model connection in notebooks and MCP Apps. The browser
integration suite supplies a controlled WebMCP registry across Chromium,
Firefox, and WebKit while exercising the packaged MCP App. Native Chromium tests
exercise real WebMCP registration and execution in MCP Apps, JupyterLab, and
marimo. They cover notebook-client isolation, owning-cell reruns, comm closure,
and kernel shutdown. Browser `_webmcp/host.ts` withdraws tools when the native
kernel connection or model comm closes while preserving rendered output.

## Workspace boundaries

```text
packages/anywidget-mcp
    Python API, sessions, widget bridge
    browser HTML composition
                |
                | imports
                v
packages/app
    AnyWidget frontend model and MCP App runtime

apps/e2e
    real MCP host and Python/browser integration tests
```

The private app package cannot import the Python composition package. Relative
imports stay inside `packages/app`. Package manifests own their dependencies
and tests. Root commands coordinate the workspace.

Vite composes the browser runtime into one HTML resource in the Python package.
Hatch includes that resource in the wheel and sdist. CI builds it once and
shares it with Python, browser integration, and distribution checks.

## From a tool call to a view

1. `_spec.scan()` describes the class or factory. `_mcp/targets.py` compiles
   its arguments with the MCP SDK and retains the native tool for registration.
   The SDK injects `Context` after validating model-supplied arguments.
2. `_factory.py` acquires a fresh widget or ordered widget sequence. A managed
   factory remains open for the widget session.
3. `_runtime.py` owns the session lease, bootstrap capability, state handle,
   unacknowledged replies, idle expiry, and cleanup task.
4. `_bridge.py` enrolls the recursively reachable graph and replaces model
   comms. `_model_connection.py` captures canonical initialization state.
5. `_attachments.py` records immutable sources and buffers. The bridge captures
   graph changes, messages, and projection at one snapshot boundary.
6. The primary result gives the model a summary and optional projected state,
   and gives the app a bootstrap capability.
7. `app.ts` accepts the result and `runtime-results.ts` claims the capability.
   `attachments.ts` materializes the versioned delivery and its dependencies.
8. `runtime-payload.ts` decodes the graph. `runtime.ts` applies launch messages,
   initializes model bindings, and renders the root view through `binding.ts`.
9. Browser comms return to Python through the ordered scheduler. Python
   validates changes and runs observers. The browser applies the complete
   response, then `context.ts` publishes its projection to the host.

[Widget bridge protocol](protocol.md) defines the wire shapes and bounds.

## Python ownership

`server.py` exposes `AnyWidgetMCP`, `attach()`, and `serve()`, plus HTTP policy.
`_widget_tools.py` registers resources and tools and composes the existing SDK
lifespan with the widget runtime. The SDK owns one lifespan per HTTP application,
so MCP connection rotation preserves widget sessions. Transport options belong
to `run()` and `streamable_http_app()`.

Target inspection belongs in `_spec/`. `_mcp/targets.py` owns MCP compilation
and registration. CLI inspection and serving retain that compiled target.
Public labels come from registered names, titles, and descriptions. Python class
paths remain runtime diagnostics.

`_factory.py` normalizes direct, awaitable, synchronous context-managed, and
asynchronous context-managed results. A non-empty sequence gets an internal
`_WidgetGroup` root. The same state selection applies to each returned widget.
Cleanup closes the enrolled graph before exiting the manager. Synchronous
acquisition and manager methods run in workers, with independent capacity for
manager exit. Cancellation joins running synchronous work before cleanup. Async
managers retain their owner task, and factories own rollback before yielding.

`_dynamic.py` executes generated Python in a fresh module and selects classes
from the final namespace. The module remains registered until every returned
widget closes or is collected, preserving module lookup for live classes.

`WidgetSession` in `_bridge.py` owns enrollment, rollback, snapshots, and graph
cleanup. It follows widget references recursively through synchronized dicts,
lists, and tuples. A live widget belongs to one session. Graph membership is
separate from pending initialization records so emitted bulk values can be
released.

`_comm.py` implements the kernel-style comm envelope using captured raw buffers.
`_model_connection.py` connects through `_models.py` bindings and captures
initial emissions. Ordinary updates use canonical widget state application and propagate
validation failures to MCP. `_notifications.py` keeps native traitlets notification batches inside the
session's snapshot boundary. `_detachment.py` coordinates graph replacement and
acknowledged model removal.

`_state.py` owns model-visible projections. Default and selected projections use
last-notified trait values. Custom projectors run against consistent live values
and cannot mutate synchronized traits. `StateProjection.watch` narrows
invalidation. `_projection_json.py` produces deterministic bounded JSON for the
model. `StateProjection.max_bytes` controls one aggregate JSON budget, independently
of rendering-data transfer.

## Attachment and replay ownership

`Attachments` in `_attachments.py` stores immutable bytes by SHA-256 identity.
Sources use UTF-8, buffer references contain raw binary, and large delivery JSON
uses the same store. Large values spill to temporary files before writing their
bytes. Chunk reads encode one bounded range as base64.

Live model values, pending snapshots, bootstrap, and retained replies keep their
attachments addressable. The runtime pins the full dependency set required by a
replay, including an oversized JSON envelope. Superseded unpinned versions are
released. Session cleanup closes every file, including incomplete uploads, and
participates in retryable disposal if a resource close fails.

Uploads are grouped by the comm operation they will feed. One operation may
contain many binary buffers. Its canonical message JSON is also uploaded and
referenced by `payload_ref`. Dispatch, cancellation, or a later operation retires
staging. An upload becomes readable after its digest verifies, and uploading
alone cannot change widget state.

Reads renew activity but preserve pending messages, removal acknowledgments,
and bootstrap replay. The browser can finish loading a large initial graph
before its first widget operation retires bootstrap retention.

The runtime retains replies until the browser acknowledges the fully applied
outer transaction. Nested initialization keeps the previous acknowledgment so
its parent attachments remain readable. Dispatch and acknowledgment watermarks
reject retired operations. Request fingerprints use hashes, and repeated
unacknowledged requests return their recorded result or error.

## Browser ownership

`app.ts` owns the SDK host connection, DOM shell, loading status, host event
handlers, and runtime replacement. It installs handlers before connecting.
`runtime-replacement.ts` joins teardown before mounting a new result.

`attachments.ts` owns delivery decoding, upload preparation, chunk retrieval,
digest verification, and a weak buffer cache. Independent read ranges use a
small concurrency window inside the active transaction. Cache hits are copied
and reverified before reuse because widget code can mutate or transfer buffers.

`assets.ts` owns source interpretation and persistent caching for `_esm` and
`_css`. Dataset buffers and JSON remain session-scoped. Source caching uses the browser's
storage quota, evicting its oldest entries when a write exceeds that quota.
Storage failures preserve active loading.

`runtime-payload.ts` decodes graph, message, and projection records.
`WidgetRuntime` in `runtime.ts` owns one model graph, initialization dependencies,
the ordered protocol scheduler, removal acknowledgments, polling, and disposal.
It holds a transaction through uploads, response hydration, and complete graph
application. A nested initializer command uses that transaction's active slot.

`binding.ts` owns module/CSS loading, initialization, rendering, child views,
source replacement, and cleanup. `module-loader.ts` bounds source evaluation to
10 seconds and releases its script elements, Blob URLs, and export callbacks on
completion or cancellation. Module scripts have distinct Blob URLs so concurrent
loads can attribute browser execution errors to their source.
`BridgeModel` in `model.ts` owns frontend values,
change events, custom messages, and pending state serialization. Each scoped model
owns its subscription wrappers so ending one view preserves other views.
Initialization and source reconciliation run until completion or lifecycle
cancellation. Source revisions yield to the event loop between attempts.
Teardown retains a deadline so a stalled widget cleanup cannot block replacement.
Custom messages emitted before initialization finishes remain available to the
first listener. Subsequent unobserved live events follow normal event semantics.

`context.ts` coalesces complete projections and publishes according to advertised
host capabilities. A shared owner epoch republishes the current projection if an
older host request settles after runtime replacement. `anywidget_state` provides
a model-visible pull path with the same Python-authoritative state.

## Lifecycle invariants

- Launch messages apply before bindings initialize, render, or publish context.
- Container replacement enrolls the new graph before its parent reference update
  reaches the browser.
- Detached models remain callable until a later poll acknowledges completed view
  teardown. Large acknowledgment sets are sent in bounded batches.
- API call order survives transfer retries. A response is fully applied before
  later queued work can mutate the graph.
- Cancellation propagates through bootstrap, uploads, reads, initialization, and
  rendering. A superseded view cannot resume rendering after its scope ends.
- Exhausted delivery or application failure disposes the runtime before another
  operation advances. The error remains visible in the app.
- App disposal, idle expiry, explicit `aclose()`, and server shutdown use the same
  graph and factory cleanup path.

[Development](development.md) lists the unit, package, and browser gates.
