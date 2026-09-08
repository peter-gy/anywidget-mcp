# Development

## Repository map

- `packages/app/src/` contains the browser MCP App runtime and host-independent
  WebMCP instrumentation.
- `packages/app/tests/` contains Vite+ tests for browser contracts.
- `apps/e2e/` contains Playwright browser tests, the MCP App host, and the
  Python widget fixture.
- `packages/anywidget-mcp/src/anywidget_mcp/` contains the public Python API,
  MCP server, state projection, widget bridge, and CLI.
- `packages/anywidget-mcp/tests/` contains Python contract tests and fixtures.
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
uv sync --all-packages --group dev --locked
pnpm install --frozen-lockfile
```

Build the packaged browser app:

```sh
pnpm --filter @anywidget-mcp/python build
```

Run the local gate:

```sh
pnpm --filter @anywidget-mcp/e2e install-browser
make check
```

The browser installation downloads Chromium, Firefox, and WebKit for
[Playwright](https://playwright.dev/docs/intro), the browser test runner.
On Linux, installing browser system dependencies may require administrator
privileges. Repeat the installation after upgrading Playwright.

## Iteration checks

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

The build provides the app resource and standalone WebMCP module read by the
Python tests.

Use one test module or test name while iterating, then return to `make check`.

## End-to-end browser tests

Build the packaged app and run the integration scenarios in Chromium, Firefox,
and WebKit:

```sh
pnpm e2e
```

Playwright starts the Python widget server and Vite host, renders the packaged
MCP App in an iframe, and stops both servers when the run finishes. The tests
exercise browser interaction against Python state through the MCP connection.

Select one browser while iterating:

```sh
pnpm e2e --project=chromium
```

After building the browser resource, run the package directly or open
Playwright's interactive test runner:

```sh
pnpm --filter @anywidget-mcp/e2e e2e --project=chromium
pnpm --filter @anywidget-mcp/e2e e2e:ui
```

`pnpm test` runs the Vite+ unit suite. `make check` runs the browser integration
suite after its build step. CI runs a separate job for each browser and uploads
`apps/e2e/playwright-report/` and `apps/e2e/test-results/` after the test run.

The browser suite transfers an 8 MiB binary value and 40,000 JSON records,
recovers a dropped attachment response, and checks the resulting Python state.
The host enforces a 128 KiB tool-message budget and applies resource content
security policy. Startup coverage includes repeated source revisions and
custom messages emitted before rendering.

## Package boundary

Build both JavaScript packages and the Python artifacts:

```sh
make package
```

The package gate performs these checks:

1. Vite+ packs `@anywidget-mcp/app`.
2. Vite+ composes the single-file HTML resource and standalone WebMCP module.
3. uv builds the sdist and wheel.
4. Twine validates both artifacts.
5. uv builds another wheel from the sdist.
6. A fresh environment imports `anywidget_mcp`, reads the packaged HTML, and
   runs `anywidget-mcp --help`.

The Hatch build hook requires both browser artifacts and reports their build
command when either is missing.

CI builds the browser packages once. The Python compatibility matrix, browser
integration matrix, and distribution job consume that browser artifact.
`make package-artifacts` checks distributions from an existing browser build,
and `make package` builds the browser packages first. CI retains the validated
distributions for the release workflow.

## Local MCP server

Serve a widget package:

```sh
uv run --package anywidget-mcp anywidget-mcp serve wigglystuff:ColorPicker
```

Inspect the tool contract produced by a class or factory:

```sh
uv run --package anywidget-mcp anywidget-mcp inspect wigglystuff:ColorPicker --json
```

The inspector and server resolve the same `MODULE:OBJECT` target and use the
same compiled registration description. Inspection reports `widget-class` or
`factory`. Explicit target parameters define widget arguments in the input
schema, and MCPServer `Context` parameters stay outside it. `anywidget-mcp` adds
optional `loading_message` for host progress text. A class with variadic
constructor parameters exposes this framework field and no widget arguments.
Use an explicit factory signature when the MCP tool accepts widget arguments.

Run the browser fixture used for protocol checks:

```sh
uv run --package anywidget-mcp python apps/e2e/server.py --port 8766
```

The fixture provides `bridge_probe` for binary state, custom commands, child
replacement, and model context. It also provides `hot_reload_probe` for ESM
and CSS replacement. `large_asset_probe` renders two models backed by one shared
three-megabyte ESM source. Every probe exercises the versioned source-asset
protocol. Initial and dynamic sources travel as content references, and the
browser reads verified source attachments in bounded `anywidget_read` chunks
before initializing bindings.

## mcp-use Inspector Chat

Start the pinned Inspector after the fixture is listening:

```sh
npx --yes @mcp-use/inspector@20.3.7 \
  --url http://127.0.0.1:8766/mcp \
  --port 8082
```

Open `http://127.0.0.1:8082/inspector?tab=chat` with one named `agent-browser`
session. Configure the OpenAI-compatible chat provider in the Inspector UI.

The default local server permits direct browser connections from HTTP loopback
origins. Check that widget calls go to the server's `/mcp` endpoint. Inspector's
proxy shares a request limit across widget traffic and chat tool calls, which
rapid input or attachment transfers can exhaust. After changing CORS settings,
reconnect the server in Inspector so it selects the direct connection.

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
  attachment range through `anywidget_read`. Transfer retries keep the same
  range and data identity.
- Check page errors, browser console errors, and failed requests.

Use a custom session ID for every `agent-browser` command. Close that session,
the Inspector, and the fixture before handoff.
