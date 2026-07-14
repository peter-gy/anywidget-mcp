# anywidget-mcp

`anywidget-mcp` is a uv and pnpm workspace for exposing AnyWidget classes and
factories as interactive MCP Apps. Python owns registration, widget sessions,
traitlets, and model-visible state. The browser app owns AnyWidget rendering
and MCP host integration. Preserve that split.

Read this file before changing the repository.

## Commands

| Purpose              | Command                                                                                                              | Expected result                                             |
| -------------------- | -------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Install Python       | `uv sync --all-packages --group dev`                                                                                 | Workspace and development dependencies resolve              |
| Install JavaScript   | `pnpm install --frozen-lockfile`                                                                                     | Vite+ workspace installs from `pnpm-lock.yaml`              |
| Check JavaScript     | `pnpm check`                                                                                                         | Formatting, linting, and type checks pass                   |
| Test browser app     | `pnpm test`                                                                                                          | Vite+ tests pass                                            |
| Test Python          | `pnpm --filter @anywidget-mcp/python build && uv run --package anywidget-mcp pytest -q packages/anywidget-mcp/tests` | Browser resource builds, then Python contract tests pass    |
| Build browser assets | `pnpm build`                                                                                                         | Package builds and the single-file app are produced         |
| Build distribution   | `make package`                                                                                                       | Wheel, sdist, wheel-from-sdist, import, and CLI checks pass |
| Full handoff gate    | `make check`                                                                                                         | Every local gate passes                                     |

Use package filters while iterating:

```sh
pnpm --filter @anywidget-mcp/app test
pnpm --filter @anywidget-mcp/python build
pnpm --filter @anywidget-mcp/python build && uv run --package anywidget-mcp pytest -q packages/anywidget-mcp/tests/test_server.py
```

## Architecture

- `packages/app/` owns the browser MCP App, AnyWidget Frontend Model
  implementation, model bindings, host context, bridge calls, and
  `updateModelContext` delivery.
- `packages/anywidget-mcp/src/anywidget_mcp/server.py` owns the public
  `WidgetTools`, `attach()`, `AnyWidgetMCP`, and `serve()` APIs, MCP resources,
  tool registration, session leases, CORS, and shutdown. Every entry point uses
  the same registration adapter.
- `packages/anywidget-mcp/src/anywidget_mcp/_bridge.py` owns widget identity,
  graph enrollment, canonical comm messages, buffers, polling, and disposal.
- `packages/anywidget-mcp/src/anywidget_mcp/_state.py` owns model-visible
  projections, observation, deterministic summaries, and projection versions.
- `packages/anywidget-mcp/` composes the private browser app into the
  single-file HTML resource shipped by the `anywidget-mcp` distribution.

See [Architecture](development_docs/architecture.md) for the complete
responsibility flow and [Development](development_docs/development.md) for
workspace and Inspector workflows.

## Dependency rule

Dependencies point from the Python composition package to
`@anywidget-mcp/app`. The app package cannot import the Python composition
package. The root Vite+ configuration rejects package imports in that direction,
and the app test suite rejects a reverse workspace dependency in its manifest.
Keep relative imports inside `packages/app`.

Python and the browser meet through four explicit protocol surfaces:

1. MCP tool arguments create a fresh Python widget graph.
2. Tool result metadata carries the full serialized model graph, queued launch
   messages, buffers, ESM, CSS, and one matching initial state projection.
3. App-visible comm tools carry canonical AnyWidget messages between the
   browser models and Python traitlets.
4. `updateModelContext` publishes the latest complete model-visible projection
   after Python validation and observers run.

Change both runtimes and their boundary tests when a message, metadata, trait,
buffer, resource, or projection shape changes.

## State invariant

Python is authoritative for synchronized widget state. Browser changes travel
through `anywidget_comm`, traitlets validate and observe them, and the response
returns every resulting model update plus one complete state projection.
The app applies those updates in order before publishing that projection to the
host.

`WidgetSession` captures messages, model membership, and model context through
one snapshot boundary. Default and selected projections serialize the last
notified trait values. Custom projectors run only when live traits match that
committed shadow, and they must not mutate synchronized traits. Launch messages
are applied before bindings initialize, render, or publish initial context.

Every comm and poll cycle keeps one operation ID through bounded transport
retries. Python replays the complete success or error for a repeated comm ID.
The runtime scheduler preserves API call order across models, custom messages,
and polls. The browser queue holds each protocol transaction until its response
is fully applied. Dynamic model initialization reuses the active transaction
for commands that it awaits. A terminal delivery or application fault disposes
the runtime before another call can change the model graph.

Each widget tool call owns a fresh root widget and every widget reference found
recursively in synchronized dicts, lists, and tuples. References use
`anywidget:<model_id>`. AnyWidget subclasses and descriptor-backed protocol
objects share one session. A live model belongs to one session. Container
replacement enrolls the new reachable graph before the parent reference update
reaches the browser. Rejected replacements restore the owning model. Browser
comms invalidate callable projections for protocol objects without trait
observers. Observable protocol objects use the session notification gate.
Disposal closes the full enrolled graph.

Registered tool names and titles are the model-facing identity. Python class
paths remain runtime diagnostics. Tool results and model context use the
`tool` field for that identity.

The browser poll schedule starts at 500 ms after activity, then backs off
through 1 s, 2 s, 5 s, 10 s, and 15 s while idle. Returned activity resets the
next poll to 500 ms. A browser comm interrupts the current idle wait, then
schedules a poll after 500 ms. Preserve queue ordering, operation IDs, and
disposal behavior when changing this schedule.

## Tooling and generated files

The root `vite.config.ts` owns Vite+ formatting, linting, type-aware checks,
architecture restrictions, and task caching. Package Vite configs own tests,
library packing, and the final single-file build.

The root `pyproject.toml` is a virtual uv workspace. Publishable metadata and
build configuration live in `packages/anywidget-mcp/pyproject.toml`. Scope
Python commands with `--package anywidget-mcp`.

These paths are generated:

- `packages/app/dist/`
- `packages/anywidget-mcp/src/anywidget_mcp/static/`
- `dist/`

Change source and rebuild. The Hatch build hook requires the browser artifact
before creating a distribution.

## Test ownership

- Python tests exercise public registration, schemas, session lifecycle,
  recursive trait serialization, protocol-backed composition, state
  projections, CLI behavior, and MCP metadata.
- App tests exercise model events, binary paths, binding lifecycle, ordered
  tool calls, host behavior, model-context delivery, and teardown.
- Packaging checks build the wheel from the sdist and import it in a fresh
  environment.
- Runtime changes finish with mcp-use Inspector Chat and `$agent-browser`.
  Exercise browser interaction, Python observer effects, model-context
  readback, custom commands, child replacement, and hot reload where affected.
  Check browser errors and close every browser session and local server.

Use observable state or protocol responses in tests. Avoid elapsed-time
assertions when an event, message, or DOM state can prove completion.

## Code and documentation

Use domain names from the public API and protocol. Keep comments for lifecycle
ordering, identity ownership, serialization shapes, external host behavior,
and reasons for bailing out. Delete comments that narrate ordinary code.

The root README teaches installation, use cases, and the public API.
`development_docs/` contains contributor architecture, protocol decisions,
and validation workflows. Keep implementation details out of the public
quickstart unless they change how a user calls the API.

## Handoff

Run `make check`, then run the Inspector browser scenarios for changes that
touch the browser app, protocol, state, sessions, or packaged HTML. Inspect the
final diff for stale generated files, old paths, cross-runtime drift, leaked
listeners, unrelated edits, and comments that restate the code.
