from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Literal

import duckdb
import uvicorn
from embedding_atlas.widget import EmbeddingAtlasWidget
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from anywidget_mcp import AnyWidgetMCP, StateProjection


def atlas_state(widget: EmbeddingAtlasWidget) -> dict[str, object]:
    reader = widget.selection(format="arrow")
    return {
        "selected_count": sum(batch.num_rows for batch in reader),
        "predicate": widget.get_state("_predicate")["_predicate"],
    }


def create_server() -> AnyWidgetMCP:
    server = AnyWidgetMCP(
        "Embedding Atlas integration",
        csp={"resourceDomains": ["data:"], "connectDomains": ["data:"]},
    )

    @server.widget(
        name="embedding_atlas",
        state=StateProjection(atlas_state, watch="_predicate"),
    )
    @contextmanager
    def embedding_atlas(
        rows: Literal[250000, 1000000] = 250000,
    ) -> Generator[EmbeddingAtlasWidget, None, None]:
        with duckdb.connect() as connection:
            table = connection.sql(
                f"""
                SELECT
                    i::INTEGER AS id,
                    (sin(i * 0.01) + (i % 4) * 3)::FLOAT AS x,
                    (cos(i * 0.013) + (i % 3) * 3)::FLOAT AS y,
                    'Group ' || (i % 4)::VARCHAR AS category,
                    'Point ' || i::VARCHAR AS text
                FROM range({rows}) AS points(i)
                """
            ).to_arrow_table()
            yield EmbeddingAtlasWidget(
                table,
                connection=connection,
                row_id="id",
                x="x",
                y="y",
                text="text",
                initial_state={
                    "charts": {
                        "embedding": {
                            "type": "embedding",
                            "title": "Embedding",
                            "data": {"x": "x", "y": "y", "text": "text"},
                        },
                        "selection": {
                            "type": "predicates",
                            "title": "Select points",
                            "items": [{"name": "First 1000", "predicate": "id < 1000"}],
                        },
                        "table": {"type": "instances", "title": "Instances"},
                    },
                },
            )

    return server


async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ready")


if __name__ == "__main__":
    app = create_server().streamable_http_app()
    app.routes.append(Route("/health", health))
    uvicorn.run(app, host="127.0.0.1", port=8766)
