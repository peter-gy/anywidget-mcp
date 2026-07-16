# Development

## Repository map

- `packages/app/src/` contains the browser MCP App runtime.
- `packages/app/tests/` contains Vite+ tests for browser contracts.
- `packages/anywidget-mcp/src/anywidget_mcp/` contains the public Python API,
  MCP server, state projection, widget bridge, and CLI.
- `packages/anywidget-mcp/tests/` contains Python and Inspector fixtures.
- `packages/anywidget-mcp/frontend/` contains the final Vite composition
  entry.
- `development_docs/` contains contributor architecture and protocol material.

Read [Releasing](releasing.md) before changing the package version, creating a
version tag, or publishing to PyPI.

Read [Architecture](architecture.md) before changing ownership or a
cross-runtime shape. Read [Widget bridge protocol](protocol.md) before changing
comm messages, metadata, buffers, model membership, or model context.

## Setup

Install both workspaces from the repository root:

```sh
uv sync --all-packages --group dev
pnpm install --frozen-lockfile
```

Build the packaged browser app:

```sh
pnpm --filter @anywidget-mcp/python build
```

Run the complete local gate:

```sh
make check
```

## Focused commands

Run browser checks while editing `packages/app`:

```sh
pnpm --filter @anywidget-mcp/app check
pnpm --filter @anywidget-mcp/app test
pnpm --filter @anywidget-mcp/app build
```

Run Python checks while editing the server or bridge:

```sh
pnpm --filter @anywidget-mcp/python build
uv run --package anywidget-mcp ruff format --check packages/anywidget-mcp
uv run --package anywidget-mcp ruff check packages/anywidget-mcp
uv run --package anywidget-mcp ty check packages/anywidget-mcp
uv run --package anywidget-mcp pyrefly check --min-severity warn
uv run --package anywidget-mcp pytest -q packages/anywidget-mcp/tests
```

The build provides the app resource read by the Python resource tests.

Use one test module or test name while iterating, then return to `make check`.

## Package boundary

Build both JavaScript packages and the Python artifacts:

```sh
make package
```

The package gate performs these checks:

1. Vite+ packs `@anywidget-mcp/app`.
2. Vite+ composes the single-file HTML resource.
3. uv builds the sdist and wheel.
4. Twine validates both artifacts.
5. uv builds another wheel from the sdist.
6. A fresh environment imports `anywidget_mcp`, reads the packaged HTML, and
   runs `anywidget-mcp --help`.

The Hatch build hook reports the build command when the HTML resource is
missing.

## Local MCP server

Serve a widget package directly:

```sh
uv run --package anywidget-mcp anywidget-mcp serve wigglystuff:ColorPicker
```

Inspect the exact tool contract produced by a class or factory:

```sh
uv run --package anywidget-mcp anywidget-mcp inspect wigglystuff:ColorPicker --json
```

The inspector and server resolve the same `MODULE:OBJECT` target and use the
same compiled registration description. Inspection reports `widget-class` or
`factory`. FastMCP `Context` parameters stay outside the input schema. A class
with variadic constructor parameters has an empty input schema. Use an explicit
factory signature when the MCP tool accepts arguments.

Run the browser fixture used for protocol checks:

```sh
uv run --package anywidget-mcp python packages/anywidget-mcp/tests/fixtures/browser_bridge_server.py --port 8766
```

The fixture provides `bridge_probe` for binary state, custom commands, child
replacement, and model context. It also provides `hot_reload_probe` for ESM
and CSS replacement. `large_asset_probe` renders two models backed by one shared
three-megabyte ESM source. Every probe exercises the versioned source-asset
protocol. Initial and dynamic sources travel as content references, and the
browser fetches missing text through `anywidget_assets` before initializing
bindings.

## mcp-use Inspector Chat

Start the pinned Inspector after the fixture is listening:

```sh
npx --yes @mcp-use/inspector@12.0.3 \
  --url http://127.0.0.1:8766/mcp \
  --port 8082 \
  --no-open
```

Open
`http://127.0.0.1:8082/inspector?server=http%3A%2F%2F127.0.0.1%3A8766%2Fmcp&tab=chat`
with one named `agent-browser` session. Configure the OpenAI-compatible chat
provider in the Inspector UI.

Exercise the affected scenarios:

- Ask Chat to invoke `bridge_probe` and report the initial payload size.
- Change the binary payload in the rendered app, then ask Chat for the latest
  synchronized size.
- Invoke the custom command and verify its text and binary response.
- Replace the child widget and verify the rendered child state.
- Invoke `nested_probe`, increment its protocol-backed child, and verify that
  the later Chat turn receives the updated nested state.
- Replace the nested container and verify that repeated references resolve to
  one enrolled model while detached models disappear.
- Invoke `hot_reload_probe`, then verify that CSS and ESM replacement fetch and
  apply their new source digests.
- Launch a second probe and verify that it starts from fresh Python state while
  previously verified source digests come from the browser cache.
- Invoke `large_asset_probe` twice. Verify that both child models render, one
  three-megabyte source digest appears in the cache, and the second launch
  reuses it.
- Inspect bridge calls and verify that each snapshot requests each missing
  asset ID once through `anywidget_assets`.
- Check page errors, browser console errors, and failed requests.

Use a custom session ID for every `agent-browser` command. Close that session,
the Inspector, and the fixture before handoff.
