# Widget bridge protocol

Protocol version 3 separates ordered widget operations from immutable payload
bytes. The browser implements the [AnyWidget frontend model
API](https://anywidget.dev/en/afm/). Python applies the widget's serialization,
trait validation, observers, and custom-message handlers.

Every app tool request and response carries bounded JSON. Sources, binary
buffers, and oversized JSON travel as attachments. The same tools work through
standard input/output and streamable HTTP.

## Discovery and launch

A registered widget tool points to its shared HTML resource through
`_meta.ui.resourceUri`. Its result repeats that metadata and contains:

- A model-facing summary and optional projected state.
- `structuredContent.tool`, plus `state` and `state_id` when projection is enabled.
- A separate text block matching
  `urn:anywidget-mcp:bootstrap:<32-lowercase-hex-capability>` exactly.

The app calls `anywidget_bootstrap(bootstrap_id, operation_id)` to claim the
capability. Repeating that operation ID replays its response. Another claimant
is rejected. The capability differs from the widget instance and state handles.
The configured session idle lifetime applies from launch onward. A `None`
lifetime delegates cleanup to explicit disposal and server shutdown.

The host may replay the primary result. The app accepts each bootstrap
capability once during its own lifetime. A new capability starts a replacement
runtime after teardown of the previous runtime.

### Reopening

Opted-in registrations add `_meta.anywidget.reopen` to the primary result:

```typescript
{
	version: 1;
	tool: string;
	arguments: Record<string, unknown>;
	mode: "manual" | "auto"; ui?: boolean;
}
```

The descriptor snapshots validated input fields and defaults before factory
acquisition, excluding injected dependencies and synthetic loading text. Its
JSON encoding is bounded to 64 KiB and lives in the host-retained result,
independently of session attachments. The app ignores unknown or malformed
reopening metadata while preserving ordinary bootstrap behavior.

The `ui` flag controls optional recovery controls independently of creation mode.
When omitted, it defaults to true for manual mode and false for automatic mode.
Automatic creation uses the normal loading status and tool-call path whether or
not controls are enabled. No recovery button is created when controls are disabled.

Unavailable bootstrap claims and live session calls return `isError: true`
with `_meta.anywidget.error: "session_unavailable"`. Other failures retain their
ordinary error behavior. The app offers recreation for a lost session. Automatic
recreation runs at most once per app lifetime, during initial materialization;
live session loss requires a reload or an explicit reopening action.

Recreation uses `tools/call` on the original named widget tool. It preserves
host policy, SDK validation, and current context injection. Creation calls have
no automatic transport retry. The returned bootstrap follows the existing
claim, replay, attachment, replacement, and disposal protocol. Host notifications
and direct responses enter the same result gate. Each new session has independent
operation IDs and state handles; old handles never redirect to a new session.
The browser carries the primary result's `state_id` into model-context updates,
retaining it through subsequent projections for that runtime.

These fields extend version 3 additively. Older results continue to bootstrap
normally but cannot offer reopening without a descriptor. No state or command
history is used to reconstruct a graph. The saved descriptor is untrusted input,
not a capability granting access beyond an ordinary tool invocation.

## Deliveries

Bootstrap, comm, and poll responses put a delivery in `_meta.anywidget`:

```typescript
type BlobRef = { id: string; byteLength: number };

type Delivery =
	| { protocolVersion: 3; instanceId: string; payload: RuntimePayload }
	| { protocolVersion: 3; instanceId: string; payloadRef: BlobRef };
```

Exactly one of `payload` and `payloadRef` is present. JSON payloads larger than
64 KiB, or values the MCP SDK cannot encode inline, use an attachment. This
includes large ordinary JSON traits and model graphs, as well as custom-message
results.

`RuntimePayload` carries ordered `models`, `messages`, `removedModelIds`,
`context`, and `contextError` as applicable. The initial payload also identifies
`rootModelId` and `loadingMessage`. `sessionIdleTimeoutMs` is present when idle
expiry is configured.

Model records contain their synchronized `state`, `bufferPaths`, and `buffers`.
A buffer slot is a `BlobRef`. A record's optional `sourceRefs` maps `_esm` and
`_css` to references containing their UTF-8 source. Incremental messages use the
same source and buffer representation. The decoder knows each attachment's
interpretation from its position in the payload.

The browser resolves the delivery and verifies every referenced source and
buffer before applying its graph changes. This hydration stays inside the
operation's queue slot. Model context is published after the complete outer
transaction succeeds, including nested commands awaited during initialization.

## Immutable attachments

An attachment ID is `sha256:<64-lowercase-hex-digest>` over its exact raw bytes.
`byteLength` is the raw length, before base64 encoding. Empty attachments use
the SHA-256 digest of an empty byte sequence.

`anywidget_read(instance_id, blob_id, offset)` returns:

```json
{
	"protocolVersion": 3,
	"id": "sha256:...",
	"offset": 0,
	"byteLength": 8000368,
	"data": "..."
}
```

The record appears directly in `_meta.anywidget`. `data` is base64 for at most
64 KiB of raw bytes starting at `offset`. An offset equal to the length returns
empty data. Invalid offsets and references outside the session are rejected.

Reads are immutable and idempotent. They renew session activity, but never
consume widget messages, acknowledge model removals, or retire bootstrap
retention. Independent ranges can be read concurrently within the active
transaction. Retry requests preserve their exact offset.

The browser checks response identity, range, total length, and the final SHA-256
digest before exposing content. Sources are decoded as UTF-8 after assembly.
Binary data becomes the frontend model's buffers. JSON is parsed after assembly.
A missing, truncated, or corrupt attachment fails the transaction.

Large Python attachments spill into session-owned temporary storage. Mutable
producer buffers are copied at capture so later mutations cannot change an
already recorded response. Base64 encoding happens per transfer chunk.

## Browser writes

`anywidget_write(instance_id, operation_id, blob_id, byte_length, offset, data)` uploads a
chunk of at most 64 KiB raw bytes. Its response reports `protocolVersion`, `id`,
`byteLength`, `received`, and `complete` directly in `_meta.anywidget`.

Writes append at the next offset. Retrying an accepted range requires identical
bytes. The completed upload becomes readable after its full digest is verified.
Uploads share the eventual comm operation ID. A dispatched operation releases
its upload staging, and a newer comm or poll retires lower abandoned upload
groups. Future operation groups remain staged. Session disposal closes every
remaining staging file.

`anywidget_comm` carries one reference to canonical message JSON:

```typescript
{
  instance_id: string;
  model_id: string;
  operation_id: number;
  payload_ref: BlobRef;
  acknowledged_operation_id?: number;
}
```

The browser uploads binary buffers, then the JSON `{ data, buffers }` containing
their references, before dispatching the comm. This envelope keeps transport
parsing independent of widget value size and nesting depth.

Python resolves the request and applies the canonical widget message once under
its operation ID. Uploading bytes does not mutate the widget. Browser trait
changes still pass through Python validation and observers before the response
is published to the host.

An abandoned upload calls
`anywidget_cancel(instance_id, operation_id, acknowledged_operation_id=0)`.
The response contains `protocolVersion`, `instanceId`, `operationId`, and
`retired: true` directly in `_meta.anywidget`. Cancellation retires that operation
ID and releases its upload staging. Repeating cancellation is idempotent.
Retirement preserves any mutation that an already-dispatched comm performed.

## Retention and replay

Attachment retention follows widget and operation ownership:

- Live models retain their current source and binary references.
- Pending delivery and the latest snapshot retain their referenced attachments.
- Bootstrap and unacknowledged operation replays pin the attachment closure needed to
  reconstruct their responses, including overflow JSON.
- Superseded unpinned versions are released during snapshot or replay cleanup.
- Disposal closes the full graph and attachment storage before exiting a managed
  factory.

The first subsequent successful widget operation retires bootstrap replay.
Attachment transfer alone keeps the bootstrap usable while a large launch is
loading.

Comm and poll operation IDs are positive safe integers assigned in dispatch
order within the session. Uploads use their eventual comm ID. Bootstrap claim
IDs remain opaque strings. Gaps from canceled work are valid.

Comm and poll retries preserve their `operation_id`. Comm fingerprints are
SHA-256 hashes rather than retained copies of request JSON. A comm replay
returns the original success or error. Reusing an ID for another request is an
error. A poll ID also binds its `acknowledged_model_ids` set.

Comm, poll, and cancel accept `acknowledged_operation_id`, defaulting to `0`.
It names the highest fully applied outer transaction. Python releases replies
and attachment pins through that sequence. The browser keeps its previous
acknowledgment while nested initialization commands run, then advances after
the complete transaction applies. Confirmed cancellation can also advance it.

An acknowledgment beyond the completed sequence is rejected. Lower retry
acknowledgments are harmless. Dispatch and acknowledgment watermarks reject
retired operation IDs even after their replay records have been released.

The browser weakly caches verified binary buffers. A cache hit is copied and
verified again because widget code can mutate or transfer a buffer. Persistent
source caching follows browser storage quota. On quota exhaustion it evicts its
oldest source entries and retries. A storage failure leaves active source
loading available.

## Graph and state ordering

Each tool invocation owns a fresh widget graph. References nested in
synchronized dicts, lists, and tuples use `anywidget:<model_id>`. Repeated
references enroll one model. A factory sequence receives an internal group root
and projects its returned widgets as `{"widgets": [state, ...]}`.

Container replacement enrolls the new reachable graph before its parent update
reaches the browser. Detached comms remain active while the app disposes their
views. Later polls acknowledge applied removals in byte-bounded batches and let
Python close those models. Unsent acknowledgments remain queued.

A snapshot captures messages, graph changes, and model-visible state at one
notification boundary. Default and selected projections use last-notified
trait values. Custom projectors run against consistent live values, remain
read-only, and are checked again before commit. `StateProjection.watch` narrows
invalidation to selected root traits.

The browser scheduler records API calls in order. It coalesces adjacent updates
for the same model. Custom messages, other models, and polls remain ordering
barriers. A dynamic initializer can await a command inside the active slot.
Exhausted delivery or response-application failure disposes the runtime before
another operation can change the graph.

Polling starts at 500 ms after activity and backs off through 1 s, 2 s, 5 s,
10 s, and 15 s while idle. Activity resets the next poll to 500 ms.

## Host and server lifetimes

The SDK owns one server lifespan per HTTP application. Individual MCP
connections borrow that application-owned runtime. A host can reconnect while
its widget instance remains active. Stdio holds the runtime for its server
lifespan. App disposal, idle expiry, `aclose()`, and shutdown share cleanup.

`anywidget_state(state_id)` reads the latest complete Python projection for the
language model. It preserves pending app messages and delivery state. The handle
expires with the widget session. Hosts with `updateModelContext` receive complete
replacement projections through that capability.

The host controls iframe policy. External scripts, workers, styles, images, and
network requests require the matching resource CSP, the browser's content
security policy. Source transfer does not grant browser permissions. Resource
metadata requests those permissions through [MCP
Apps](https://modelcontextprotocol.io/extensions/apps/overview).

## Recovery failures

The shell distinguishes rejected creation (`reopen_rejected`), an unconfirmed
transport outcome (`reopen_unconfirmed`), and failure to mount a recreated graph
(`reopen_mount_failed`). These codes appear on the status element's
`data-error-code`. A terminal runtime fault uses `runtime_stopped` and retains its
cause. It is distinct from advisory renderer or model-context errors. Only a
recognized unavailable session can initiate automatic recreation.

A delivered MCP cancellation unwinds managed acquisition through its existing
owner. The SDK's stateless HTTP transport creates a dispatcher per request, so a
separate cancellation notification cannot reliably address acquisition in another
request. The browser still closes and ignores late replies. Once an abandoned
factory completes, its unclaimed graph follows normal idle expiry and server
lifespan cleanup. Application acquisition must bound its own external I/O.
