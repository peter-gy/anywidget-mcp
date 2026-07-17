# Widget bridge protocol

## Decision

Use an adapter-owned comm for each AnyWidget model. The MCP App browser runtime
implements the AnyWidget Frontend Model API and carries canonical ipywidgets
messages and content-addressed widget sources through five app-only MCP
tools.

Python applies each widget's state contract, including trait validation,
serialization, observers, and custom commands when the model provides them.
The app loads ESM and CSS, renders views, and forwards model events through
canonical comm messages.

## Prototype results

The comparison used wigglystuff 0.5.13 with `ColorPicker` and `SortableList`
plus test widgets for binary state and custom messages.

| Prototype           | Observed behavior                                                                                                                                                                        | Result                          |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| Snapshot export     | Rendered initial JSON. Browser edits left Python state unchanged. The ColorPicker snapshot was 2,628 bytes and the SortableList snapshot was 12,677 bytes.                               | Suitable for static rendering   |
| Direct trait mirror | Updated string and list values. It bypassed trait serializers, emitted redundant updates for inbound values, failed JSON encoding for a `Bytes` trait, and lacked a custom-message path. | Too narrow for existing widgets |
| Comm tunnel         | Preserved serialized state, binary paths, echo locking, observers, state requests, and custom messages.                                                                                  | Selected                        |

The comm prototype produced these traces:

- Updating `ColorPicker.color` to `#abcdef` stored the value in Python, fired its observer once, emitted `echo_update` for `color`, then emitted an `update` for the observer-derived trait.
- Reordering `SortableList.value` updated Python with the new list and fired its observer once.
- A four-byte trait used `buffer_paths=[["payload"]]` and one binary part. The MCP envelope encoded that part as base64.
- An AnyWidget experimental command received its custom message and binary part, then returned a custom response with its output buffer.
- A parent serialized widget references nested in dicts, lists, and tuples as
  `anywidget:<model_id>`. Repeated references enrolled one model. A
  descriptor-backed protocol child used the same session and comm path.

## Protocol mapping

| AnyWidget or MCP concept | Adapter mapping                                                                                                                                         |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Widget construction      | A class creates one AnyWidget. A factory returns one AnyWidget or a non-empty ordered sequence. Managed factories remain active until session teardown. |
| Request context          | FastMCP injects `Context` after compiling the model-facing input schema                                                                                 |
| Initialization status    | Generated `loading_message` input reaches the app through `ui/notifications/tool-input` and the bootstrap response                                      |
| Initial state            | `widget.send_state()` emits canonical state, buffer paths, buffers, and widget sources                                                                  |
| Runtime bootstrap        | A machine text block carries a 32-hex capability. `anywidget_bootstrap` claims it and returns the versioned runtime payload.                            |
| Widget source            | `_esm` and `_css` become typed SHA-256 references. `anywidget_assets` returns session-owned source text for browser verification.                       |
| Browser comm             | `anywidget_comm` delivers an ipywidgets `update` or `custom` message with an idempotent operation ID                                                    |
| Python state change      | Comm output is returned immediately or collected by an idempotent `anywidget_poll` cycle                                                                |
| Model state read         | `anywidget_state` accepts a launch `state_id` and returns the latest complete projection without draining app updates                                   |
| Child composition        | References nested in synchronized dicts, lists, and tuples share the session and AFM host. Replacements enroll before the parent update is applied.     |
| Child removal            | The browser acknowledges applied removals through a later `anywidget_poll`, then Python closes the detached models.                                     |
| App teardown             | `anywidget_dispose` closes every enrolled model and exits the factory manager                                                                           |
| HTTP runtime ownership   | The Starlette application lifespan retains widget sessions across MCP connection rotation                                                               |
| MCP App discovery        | Tool `_meta.ui.resourceUri` points to `ui://anywidget-mcp/app.html`                                                                                     |
| Internal tool discovery  | `_meta.ui.visibility=["app"]` keeps bootstrap, assets, comm, poll, and disposal app-only. `anywidget_state` is model-only                               |
| Live model context       | `ui/update-model-context` replaces the previous state projection                                                                                        |

The primary tool result separates host discovery, model output, and app
bootstrap. Its top-level `_meta` contains a single `ui` object with
`resourceUri`. Its content contains the model-facing summary and a separate
text block whose complete value has the form
`urn:anywidget-mcp:bootstrap:<32-lowercase-hex-capability>`. The capability is
distinct from the widget `instanceId`. `structuredContent` identifies the
registered tool and carries the optional initial state projection. Projected
results also carry a distinct `state_id`.

The browser parses the capability from that exact marker and calls the app-only
`anywidget_bootstrap` tool with `bootstrap_id` and `operation_id`. That direct
response `_meta.anywidget` is the sole full runtime payload. It carries
`protocolVersion: 1`, `instanceId`, `rootModelId`, `sessionIdleTimeoutMs`,
`assetManifest`, `models`, ordered `messages`, `loadingMessage`, and optional
`context`. Model and message payloads use `sourceRefs` for ESM and CSS. The app
verifies and hydrates those references before registering the launch graph or
applying a transaction. Tool results and model context use the `tool` field for
the registered identity. After interaction, the app sends Python-authoritative
projections through `ui/update-model-context`.

Hosts that omit `ui/update-model-context` retain a model-visible pull path.
The launch summary tells the model to pass `state_id` to `anywidget_state`
before answering later questions about the widget. The tool reads the retained
Python-authoritative projection under the session protocol lock. It does not
consume messages, pending models, removals, or the projection waiting for the
app. The state handle shares the widget session lifetime and is revoked during
disposal, idle expiry, launch failure, or server shutdown.

Registration adds an optional `loading_message` tool argument when the target
has no parameter with that name. The MCP Apps host sends complete arguments
through `ui/notifications/tool-input`, which lets the app show the message
before the launch result arrives. Python normalizes the value, removes a
generated argument before target construction, and repeats the selected text in
the bootstrap response as `_meta.anywidget.loadingMessage`. A target-defined
`loading_message` keeps its schema and receives its validated value. The browser
treats the bootstrap response value as authoritative when it mounts after tool
execution.

Hosts may deliver the same primary tool result more than once. The app records
each accepted bootstrap capability for its lifetime. It resolves and mounts the
first delivery, ignores later deliveries for that capability, and accepts a
result with a new capability as a runtime replacement. The first bootstrap
operation ID claims the capability. Repeating that ID replays the same response,
while another operation ID is rejected. A host replay cannot reapply launch
messages or dispose the active Python session.

A sequence result receives one internal group model as its wire root. The
returned widgets become child models in sequence order and render through that
root. Each emitted projection applies the public `state` specification to every
returned widget, then the adapter publishes `{"widgets": [state, ...]}`. A
one-item sequence keeps the aggregate shape, so the factory's result shape
determines the state contract.

## Source asset protocol

Source IDs use `esm:sha256:<digest>` or `css:sha256:<digest>`. The digest covers
the protocol namespace, source kind, and exact UTF-8 text. The manifest records
the source kind and byte length for every reference in one snapshot.

The browser validates the manifest before requesting assets. It checks memory
and the Cache API first, batches the missing IDs through `anywidget_assets`, and
verifies each returned digest and byte length before caching the source. Every
referenced ID must appear in the manifest, and every manifest entry must be
referenced by that snapshot. Inline `_esm` and `_css` values are invalid in a
versioned wire payload. Cache reads accept the runtime abort signal and have a
500 ms deadline. Cache writes start after verified source enters memory and do
not delay source hydration.

The browser memory cache retains at most 32 MiB of verified UTF-8 source text
and evicts the least-recently-used entries. A larger source still hydrates the
active transaction and remains eligible for browser Cache API storage.

Launch state, queued launch messages, comm responses, poll responses, dynamic
models, and live `_esm` or `_css` updates use the same source-reference shape.
The browser resolves the complete set before it mutates the model graph. This
keeps asset loading inside the protocol transaction that introduced the source.

The Python session retains the last emitted source for each live model plus
every source referenced by the latest snapshot. Bounded comm and poll replay
entries pin the sources required by their stored responses. The app hydrates a
snapshot before it issues another comm or poll. Each ordered snapshot releases
unpinned superseded versions. Replay eviction releases versions retained by
that replay. The bootstrap replay pins its launch sources until the first later
successful app call retires the replay. Asset requests are repeatable during the
active transaction and do not consume registry entries.

The current wire contract uses `protocolVersion: 1`. The field is required on
bootstrap, comm, poll, and asset responses. The browser rejects a payload whose
version differs from its runtime contract.

Each browser comm and poll cycle keeps one `operation_id` across bounded
transport retries. The session caches complete comm responses for recent IDs
and the complete response for the latest poll ID. Cached comm outcomes include
application errors. A retry therefore receives the messages, model changes,
projection, or error produced by the first application. Reusing a comm ID with
another payload is a protocol error. A poll operation ID also binds its
`acknowledged_model_ids` set.

The browser polls after 500 ms of activity, then backs off through 1 s, 2 s,
5 s, 10 s, and 15 s while idle. A response with activity resets the next poll
to 500 ms. A browser comm interrupts the current idle wait, then schedules a
poll after 500 ms.

Each session snapshot waits for a completed trait notification batch, then
captures messages, graph changes, and context together. Default and selected
context uses last-notified values. A custom projector runs only when every
observed live trait matches its last-notified value, and a second check rejects
concurrent drift before commit. Projection callables must not mutate
synchronized traits.

`StateProjection` can narrow invalidation to selected traits on each projection
root. A sequence applies the same `watch` selection to every returned widget.
The state runtime still observes the enrolled graph while the projector runs so
mutation checks and committed-shadow validation remain intact. `watch=None`
invalidates from the complete graph, while an empty watch set publishes the
initial projection once.

The runtime scheduler records browser comms and polls at API-call time. It
coalesces only adjacent updates for the same model. Custom messages, another
model, and polls preserve their position as ordering barriers. `ToolCallQueue`
then holds one global slot through transport retries and complete response
application. Calls cannot overtake an unacknowledged response.

Dynamic model initialization may await an experimental command. That command
uses the active queue slot and applies its nested response before initialization
continues. Exhausting delivery retries or failing to apply a response disposes
the runtime because the browser can no longer prove that its model graph
matches Python.

## Runtime ownership

The stdio transport holds one widget runtime for its server lifespan. The
streamable HTTP adapter also acquires that runtime from the Starlette
application lifespan. Individual MCP connections may enter and leave the
FastMCP lifespan while the application reference keeps the application-level
session registry active for the serving process. This applies to
`AnyWidgetMCP` and to a `FastMCP` server configured through `attach()`.

An `instance_id` therefore remains valid when a streamable HTTP host opens a
new MCP connection. `anywidget_dispose`, idle expiry, `aclose()`, and
application shutdown remove the session through the same cleanup path.

An unclaimed launch expires after the shorter of 30 seconds and the configured
session idle timeout. A claimed bootstrap response reports that configured
timeout as `sessionIdleTimeoutMs` so the browser can align its lifecycle with
the Python lease.

Python keeps a detached model comm active after announcing its ID in
`removedModelIds`. The browser initializes newly announced models, applies
messages, disposes removed bindings, then records those IDs for acknowledgment.
A later poll snapshots the recorded IDs when that poll begins. Python validates
the complete set and closes those detached models before taking the poll
snapshot. A poll already queued inside an active transaction carries the older
acknowledgment set, so transient model initializers can finish against their
live comm.

## Reference alignment

- anywidget 0.11 defines the browser-facing model API and ESM lifecycle used by the runtime.
- ipywidgets 8.1 defines the comm messages used for `update`, `echo_update`, `request_state`, and `custom`.
- MCP Apps 2026-01-26 defines nested UI metadata, app-visible tools, resource CSP, permissions, and `text/html;profile=mcp-app`.
- The official Python MCP SDK provides the FastMCP tool and resource metadata used by the server.
- mcp-use provides the pinned Inspector used for host-side testing. Inspector 12.0.3 replaces tool metadata with result metadata while rendering a result, so launch results mirror the canonical nested resource URI. It handles `ui/update-model-context` and uses that state in later chat turns, but omits the capability from its initialization response. The app recognizes this Inspector host and sends complete text and structured state snapshots.

## Resource and model graph boundaries

Verified inline ESM runs in an appended module script. URL-based ESM and CSS
retain their original URL after source hydration and require their origins in
the resource CSP. The app resource includes `blob:` for widgets that construct
blob-backed modules at runtime. Camera, microphone, geolocation, and clipboard
access require matching resource permissions and host approval.

The factory's child graph is enrolled at launch. A sequence adds its returned
widgets beneath one internal group root. Graph discovery recursively walks
every synchronized dict, list, and tuple. Replacing a container enrolls the new
reachable graph in the current session. Detached models close after the browser
acknowledges their applied removal in a later poll.
