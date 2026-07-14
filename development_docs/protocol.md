# Widget bridge protocol

## Decision

Use an adapter-owned comm for each AnyWidget model. The MCP App browser runtime
implements the AnyWidget Frontend Model API and carries canonical ipywidgets
messages through three app-visible MCP tools.

Python applies each widget's state contract, including trait validation,
serialization, observers, and custom commands when the model provides them.
The app loads ESM and CSS, renders views, and forwards model events through
canonical comm messages.

## Prototype results

The comparison used wigglystuff 0.5.13 with `ColorPicker` and `SortableList` plus focused binary and custom-message widgets.

| Prototype           | Observed behavior                                                                                                                                                                          | Result                          |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------- |
| Snapshot export     | Rendered initial JSON. Browser edits left Python state unchanged. The ColorPicker snapshot was 2,628 bytes and the SortableList snapshot was 12,677 bytes.                                 | Suitable for static rendering   |
| Direct trait mirror | Updated simple strings and lists. It bypassed trait serializers, emitted redundant updates for inbound values, failed JSON encoding for a `Bytes` trait, and lacked a custom-message path. | Too narrow for existing widgets |
| Comm tunnel         | Preserved serialized state, binary paths, echo locking, observers, state requests, and custom messages.                                                                                    | Selected                        |

The comm prototype produced these concrete traces:

- Updating `ColorPicker.color` to `#abcdef` stored the value in Python, fired its observer once, emitted `echo_update` for `color`, then emitted an `update` for the observer-derived trait.
- Reordering `SortableList.value` updated Python with the new list and fired its observer once.
- A four-byte trait used `buffer_paths=[["payload"]]` and one binary part. The MCP envelope encoded that part as base64.
- An AnyWidget experimental command received its custom message and binary part, then returned a custom response with its output buffer.
- A parent serialized widget references nested in dicts, lists, and tuples as
  `anywidget:<model_id>`. Repeated references enrolled one model. A
  descriptor-backed protocol child used the same session and comm path.

## Protocol mapping

| AnyWidget or MCP concept | Adapter mapping                                                                                                                                     |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Widget construction      | Class or factory call creates one session-scoped root model                                                                                         |
| Initial state            | `widget.send_state()` emits canonical state, buffer paths, and buffers                                                                              |
| Browser comm             | `anywidget_comm` delivers an ipywidgets `update` or `custom` message with an idempotent operation ID                                                |
| Python state change      | Comm output is returned immediately or collected by an idempotent `anywidget_poll` cycle                                                            |
| Child composition        | References nested in synchronized dicts, lists, and tuples share the session and AFM host. Replacements enroll before the parent update is applied. |
| App teardown             | `anywidget_dispose` closes every enrolled model                                                                                                     |
| MCP App discovery        | Tool `_meta.ui.resourceUri` points to `ui://anywidget-mcp/widget.html`                                                                              |
| Internal tool discovery  | `_meta.ui.visibility=["app"]` keeps session tools app-facing                                                                                        |
| Live model context       | `ui/update-model-context` replaces the previous concise state projection                                                                            |

The runtime payload lives in the tool result `_meta`. `content` gives the model
an initial state projection. `structuredContent` identifies the registered tool
and exposes that projection to the app. ESM, CSS, binary parts, model IDs,
ordered launch messages, and the session ID stay in `_meta.anywidget`. The app
registers the full launch graph and applies its messages before initializing
bindings or publishing context. Tool results and model context use the `tool`
field for the registered identity. After interaction, the app sends
Python-authoritative projections through `ui/update-model-context`.

Each browser comm and poll cycle keeps one `operation_id` across bounded
transport retries. The session caches complete comm responses for recent IDs
and the complete response for the latest poll ID. Cached comm outcomes include
application errors. A retry therefore receives the messages, model changes,
projection, or error produced by the first application. Reusing a comm ID with
another payload is a protocol error.

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

## Reference alignment

- anywidget 0.11 defines the browser-facing model API and ESM lifecycle used by the runtime.
- ipywidgets 8.1 defines the comm messages used for `update`, `echo_update`, `request_state`, and `custom`.
- MCP Apps 2026-01-26 defines nested UI metadata, app-visible tools, resource CSP, permissions, and `text/html;profile=mcp-app`.
- The official Python MCP SDK provides the FastMCP tool and resource metadata used by the server.
- mcp-use provides the pinned Inspector used for host-side testing. Inspector 12.0.3 replaces tool metadata with result metadata while rendering a result, so launch results mirror the canonical nested resource URI. It handles `ui/update-model-context` and uses that state in later chat turns, but omits the capability from its initialization response. The app recognizes this Inspector host and sends complete text and structured state snapshots.

## Resource and model graph boundaries

Source ESM loads from a `blob:` URL. URL-based ESM and CSS require their origins in the resource CSP. Camera, microphone, geolocation, and clipboard access require matching resource permissions and host approval.

The factory's child graph is enrolled at launch. Graph discovery recursively
walks every synchronized dict, list, and tuple. Replacing a container enrolls
the new reachable graph in the current session and closes detached models.
