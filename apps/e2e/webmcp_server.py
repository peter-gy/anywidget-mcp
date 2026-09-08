from __future__ import annotations

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
    _esm = """
    export default {
      render({ model, el, signal }) {
        const output = document.createElement("output");
        output.setAttribute("aria-label", "Counter state");
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
      }
    };
    """

    @traitlets.observe("value")
    def _double(self, change: traitlets.Bunch) -> None:
        self.set_trait("doubled", change.new * 2)


async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ready")


def main() -> None:
    webmcp.enable()
    try:
        server = AnyWidgetMCP("WebMCP composition fixture")
        server.widget(Counter, name="webmcp_counter", state=("value", "doubled"))
        app = server.streamable_http_app()
        app.routes.append(Route("/health", health))
        uvicorn.run(app, host="127.0.0.1", port=8767)
    finally:
        webmcp.disable()


if __name__ == "__main__":
    main()
