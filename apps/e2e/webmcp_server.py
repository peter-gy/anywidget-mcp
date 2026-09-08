from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anywidget
import traitlets
import uvicorn
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from anywidget_mcp import webmcp
from anywidget_mcp.server import AnyWidgetMCP


class Counter(anywidget.AnyWidget):
    value = traitlets.Int(1, min=0).tag(sync=True)
    doubled = traitlets.Int(2, read_only=True).tag(sync=True)
    _esm = traitlets.Unicode("""
    export default () => new class {
      initialize({ model }) {
        this.model = model;
        return new class {
          current() { return model.get("value"); }
        };
      }
      get render() {
        if (!this.model) throw new Error("Counter must initialize before rendering");
        const model = this.model;
        return ({ el, signal }) => {
        const output = document.createElement("output");
        output.setAttribute("aria-label", "{{counter_label}}");
        const draw = () => {
          output.value = `${model.get("value")} / ${model.get("doubled")}`;
        };
        model.on("change:value change:doubled", draw);
        draw();
        const increment = document.createElement("button");
        increment.textContent = "Increment";
        increment.addEventListener("click", () => {
          model.set("value", model.get("value") + 1);
          model.save_changes();
        }, { signal });
        el.append(output, increment);
        return () => model.off("change:value change:doubled", draw);
        };
      }
    };
    """).tag(
        sync=True,
        to_json=lambda source, _widget: source.replace(
            "{{counter_label}}", "Counter state"
        ),
    )

    @traitlets.observe("value")
    def _double(self, change: traitlets.Bunch) -> None:
        self.set_trait("doubled", change.new * 2)


async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ready")


def main() -> None:
    workspace_lock = asyncio.Lock()

    @asynccontextmanager
    async def counter() -> AsyncGenerator[Counter, None]:
        async with workspace_lock:
            session = webmcp.enable()
            try:
                yield Counter()
            finally:
                session.close()

    @asynccontextmanager
    async def workspace() -> AsyncGenerator[anywidget.AnyWidget, None]:
        async with workspace_lock:
            created: list[Counter] = []

            class ConfigurableCounter(Counter):
                secret = traitlets.Unicode("private workspace note").tag(sync=True)

                def __init__(self, start: int = 1) -> None:
                    super().__init__(value=start)
                    created.append(self)

            async def create_counter(start: int = 4, locked: bool = False) -> Counter:
                """Create an interactive counter at the requested value."""
                widget = ConfigurableCounter(start=start)
                if locked:
                    webmcp.enable(widgets={widget: {"read_only": True}})
                return widget

            session = webmcp.enable(
                widgets={
                    ConfigurableCounter: {"name": "counter", "private": {"secret"}},
                    create_counter: {},
                },
                discover=False,
            )
            setattr(session, "_fixture_created", created)
            try:
                yield session
            finally:
                session.close()

    try:
        server = AnyWidgetMCP("WebMCP composition fixture")
        server.widget(counter, name="webmcp_counter", state=("value", "doubled"))
        server.widget(
            workspace,
            name="webmcp_workspace",
            state=lambda session: {
                "created": len(session._fixture_created),
                "counters": [
                    {"value": widget.value, "doubled": widget.doubled}
                    for widget in session._fixture_created
                ],
            },
        )
        app = server.streamable_http_app()
        app.routes.append(Route("/health", health))
        uvicorn.run(app, host="127.0.0.1", port=8767)
    finally:
        webmcp.disable()


if __name__ == "__main__":
    main()
