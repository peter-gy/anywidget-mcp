# Work with large widgets

Widget data and model-visible state have separate contracts. The browser
receives complete synchronized values and custom-message results. The `state`
option chooses a bounded description for the language model, such as a filter,
selection, or summary.

`anywidget-mcp` transfers large JavaScript modules, binary buffers, and JSON in
chunks automatically. Your widget keeps its ordinary AnyWidget API. Python
validates browser changes before the app publishes model-visible state.

## Keep queries with the data

[Embedding Atlas](https://apple.github.io/embedding-atlas/) is a viewer for
exploring projected datasets. Its Python widget uses
[DuckDB](https://duckdb.org/docs/stable/), an in-process query engine, and returns
query results as [Arrow](https://arrow.apache.org/), a columnar data format.
The dataset stays in Python while the browser requests what the view needs.

Install the packages in your server environment:

```sh
uv pip install anywidget-mcp 'embedding-atlas==0.24.0'
```

Atlas 0.24 uses [WebGPU](https://developer.mozilla.org/en-US/docs/Web/API/WebGPU_API)
for GPU rendering and requires its `shader-f16` capability. Its background
workers run [WebAssembly](https://webassembly.org/), a browser binary execution
format, and load embedded code from data URLs. Use an MCP Apps
host that enables those browser capabilities. The app policy declares data URLs
and requests WebAssembly compilation through `scriptDirectives`, a host extension
supported by
[mcp-use Inspector](https://github.com/mcp-use/mcp-use/tree/main/libraries/typescript/packages/inspector)
20.3.7. Other hosts must provide equivalent WebAssembly permission:

```python
from collections.abc import Generator
from contextlib import contextmanager

import duckdb
from embedding_atlas.widget import EmbeddingAtlasWidget

from anywidget_mcp import AnyWidgetMCP, StateProjection

mcp = AnyWidgetMCP(
    "Dataset tools",
    csp={
        "resourceDomains": ["data:"],
        "connectDomains": ["data:"],
        "scriptDirectives": ["'wasm-unsafe-eval'"],
    },
)


@mcp.widget(
    state=StateProjection(
        lambda widget: {"filter": widget.get_state("_predicate")["_predicate"]},
        watch="_predicate",
    ),
)
@contextmanager
def explore_points() -> Generator[EmbeddingAtlasWidget, None, None]:
    """Explore 250,000 points and share the current filter."""
    with duckdb.connect() as connection:
        points = connection.sql("""
            SELECT i::INTEGER AS id,
                   sin(i * 0.01)::FLOAT AS x,
                   cos(i * 0.013)::FLOAT AS y,
                   'Point ' || i::VARCHAR AS text
            FROM range(250000) AS points(i)
        """).to_arrow_table()
        yield EmbeddingAtlasWidget(
            points, connection=connection, row_id="id", x="x", y="y", text="text"
        )


mcp.run(transport="streamable-http")
```

The host connects to `http://127.0.0.1:8000/mcp`. Open `explore_points`, filter
the view, then ask the model about the filter. Atlas synchronizes its selection
predicate in `_predicate`. The projection gives that value the model-facing name
`filter` and recomputes when the predicate changes. The managed factory keeps
DuckDB open until the widget session closes.

For your own dataset, replace the query that creates `points` with your server's
data-loading code. Preserve precomputed `x` and `y` columns when you already have
an embedding.

## Bound transport messages, preserve widget values

Each attachment has an immutable identity and byte length. The app reads bounded
chunks and checks the completed content before giving it to the widget. A lost
chunk can be retried while the same operation remains active. The app applies
the complete response before it publishes the resulting model-visible state.

Large browser updates use the same attachment mechanism in the other direction.
This covers binary input and large JSON values, including selections returned
by a widget. Chunking works over both supported MCP transports.

Source modules and styles can use persistent browser caching. Dataset buffers
and JSON remain scoped to the active session. Large server-side attachments
spill into temporary storage, which is released through session cleanup.

## Own the session lifetime

Uploads belong to the operation that uses them. Completing or canceling an
operation releases its staging files. The browser acknowledges fully applied
responses so Python can release their retained attachments. Provision server
temporary storage and browser memory for your datasets.

Sessions expire after 15 minutes of inactivity by default. For a workspace whose
server owns the complete lifetime, configure explicit cleanup:

```python
mcp = AnyWidgetMCP("Dataset tools", session_idle_timeout=None)
```

App disposal, `await mcp.aclose()`, or server shutdown then closes the widget
graph and its storage. Rendering capacity depends on the widget, available
server/browser memory, and browser capabilities. A host
must permit the widget's scripts, workers, and network destinations through its
[app policy](./deployment#configure-browser-and-app-policy).

Choose [model-visible state](./state) for what the model needs to reason about.
Keep complete rendering data in the widget's synchronized traits or native query
protocol.
