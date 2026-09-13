"""Managed SQLite explorer for saved-result and server-restart scenarios."""

from __future__ import annotations

import argparse
import sqlite3
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Literal

import anywidget
import anyio
import traitlets as t
import uvicorn
from anywidget.experimental import command
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from anywidget_mcp.server import AnyWidgetMCP

EVENTS: list[dict[str, Any]] = []


class Fault(BaseModel):
    mode: Literal["none", "reject", "crash", "render", "wait"] = "none"


FAULT = Fault()
WAITERS: set[anyio.Event] = set()


class Query(BaseModel):
    values: list[int] = Field(min_length=1)


class Explorer(anywidget.AnyWidget):
    resource = t.Unicode().tag(sync=True)
    selection = t.Int(0).tag(sync=True)
    selected_value = t.Int(0).tag(sync=True)
    exports = t.Int(0).tag(sync=True)
    _esm = """
    export default {
      render({ model, el, experimental, signal }) {
        el.innerHTML = `<p>SQLite data explorer</p>
          <output data-testid="resource"></output>
          <output data-testid="selected-value"></output>
          <output data-testid="exports"></output>
          <button data-testid="next">Next row</button>
          <button data-testid="export">Export summary</button>`;
        const update = () => {
          el.querySelector('[data-testid="resource"]').textContent = model.get("resource");
          el.querySelector('[data-testid="selected-value"]').textContent = String(model.get("selected_value"));
          el.querySelector('[data-testid="exports"]').textContent = String(model.get("exports"));
        };
        model.on("change", update);
        update();
        el.querySelector('[data-testid="next"]').onclick = () => {
          model.set("selection", model.get("selection") + 1);
          model.save_changes();
        };
        el.querySelector('[data-testid="export"]').onclick = async () => {
          await experimental.invoke("export_summary", {}, { signal });
        };
        return () => model.off("change", update);
      }
    };
    """

    @command
    def export_summary(
        self, _message: Any, _buffers: Any
    ) -> tuple[dict[str, Any], list[bytes]]:
        self.exports += 1
        EVENTS.append({"event": "export", "resource": self.resource})
        return {"value": self.selected_value}, []


def create_server(session_idle_timeout: float = 30) -> AnyWidgetMCP:
    server = AnyWidgetMCP("SQLite explorer", session_idle_timeout=session_idle_timeout)

    @server.widget(name="explore_manual", reopen="manual")
    @server.widget(name="explore_auto", reopen="auto", reopen_ui=True)
    @server.widget(name="explore_quiet", reopen="auto")
    @asynccontextmanager
    async def explore(
        query: Query, ctx: Context, scale: int = 2
    ) -> AsyncGenerator[Explorer, None]:
        if FAULT.mode == "reject":
            raise ToolError("Dataset readings is unavailable. Choose another dataset.")
        if FAULT.mode == "crash":
            raise RuntimeError("internal-credential-must-not-leak")
        resource = uuid.uuid4().hex
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.execute("create table readings (value integer)")
        connection.executemany(
            "insert into readings values (?)", [(n * scale,) for n in query.values]
        )
        widget = Explorer(resource=resource)
        if FAULT.mode == "render":
            widget._esm = 'export default {render() {throw new Error("Explorer rendering failed");}};'

        def select(_change: Any = None) -> None:
            row = connection.execute(
                "select value from readings limit 1 offset ?",
                (widget.selection % len(query.values),),
            ).fetchone()
            widget.selected_value = row[0]

        widget.observe(select, "selection")
        select()
        EVENTS.append(
            {"event": "open", "resource": resource, "request": ctx.request_id}
        )
        yielded = False
        gate = anyio.Event()
        try:
            if FAULT.mode == "wait":
                WAITERS.add(gate)
                await gate.wait()
            yielded = True
            yield widget
        finally:
            WAITERS.discard(gate)
            if not yielded:
                widget.close()
            connection.close()
            EVENTS.append(
                {
                    "event": "close",
                    "resource": resource,
                    "widget_closed": widget.comm is None,
                }
            )

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument(
        "--mode", choices=["stateless", "stateful"], default="stateless"
    )
    parser.add_argument("--idle-timeout", type=float, default=30)
    args = parser.parse_args()
    server = create_server(args.idle_timeout)
    app = server.streamable_http_app(
        stateless_http=args.mode == "stateless", json_response=True
    )

    async def events(_request: Request) -> JSONResponse:
        return JSONResponse(EVENTS)

    async def fault(request: Request) -> JSONResponse:
        global FAULT
        FAULT = Fault.model_validate(await request.json())
        if FAULT.mode != "wait":
            for gate in WAITERS:
                gate.set()
        return JSONResponse(FAULT.model_dump())

    app.routes.append(Route("/events", events))
    app.routes.append(Route("/fault", fault, methods=["POST"]))
    uvicorn.run(app, host="127.0.0.1", port=args.port)
