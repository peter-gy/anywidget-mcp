from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import Any, cast

import anywidget
import traitlets
import uvicorn
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from anywidget._descriptor import MimeBundleDescriptor
from anywidget.experimental import command

from anywidget_mcp import create_anywidget
from anywidget_mcp.server import AnyWidgetMCP


def _large_leaf_module() -> str:
    source = """
    export default {
      render({ model, el }) {
        const output = document.createElement("output");
        output.dataset.testid = "large-asset-leaf";
        output.value = model.get("label");
        el.append(output);
      },
    };
    """
    target_bytes = 3 * 1024 * 1024
    padding_bytes = target_bytes - len(source.encode("utf-8")) - len("\n/**/")
    return f"{source}\n/*{'x' * padding_bytes}*/"


class LargeAssetLeaf(anywidget.AnyWidget):
    _esm = _large_leaf_module()

    label = traitlets.Unicode().tag(sync=True)


class LargeAssetProbe(anywidget.AnyWidget):
    _esm = """
    export default {
      async render({ model, el, host, signal }) {
        const status = document.createElement("output");
        status.dataset.testid = "large-asset-status";
        status.value = "loading";
        el.append(status);

        for (const name of ["first", "second"]) {
          const childRoot = document.createElement("div");
          childRoot.dataset.testid = `large-asset-${name}`;
          el.append(childRoot);
          const child = await host.getWidget(model.get(name));
          await child.render({ el: childRoot, signal });
        }
        status.value = "ready";
      },
    };
    """

    first = anywidget.WidgetTrait().tag(sync=True)
    second = anywidget.WidgetTrait().tag(sync=True)


class ChildWidget(anywidget.AnyWidget):
    _esm = """
    export default {
      initialize({ model }) {
        return { getValue: () => model.get("value") };
      },
      render({ model, el }) {
        const output = document.createElement("output");
        output.dataset.testid = "child-value";
        const update = () => { output.value = String(model.get("value")); };
        model.on("change:value", update);
        update();
        el.append(output);
        return () => model.off("change:value", update);
      },
    };
    """

    value = traitlets.Int(7).tag(sync=True)


class ProtocolChild:
    _repr_mimebundle_ = MimeBundleDescriptor(
        _esm="""
        export default {
          render({ model, el }) {
            const output = document.createElement("output");
            output.dataset.testid = "protocol-value";
            const update = () => { output.value = String(model.get("value")); };
            model.on("change:value", update);
            update();

            const increment = document.createElement("button");
            increment.dataset.testid = "increment-protocol";
            increment.textContent = "Increment protocol child";
            increment.addEventListener("click", () => {
              model.set("value", model.get("value") + 1);
              model.save_changes();
            });

            el.append(output, increment);
            return () => model.off("change:value", update);
          },
        };
        """,
        autodetect_observer=False,
    )

    def __init__(self, value: int = 3) -> None:
        self.value = value

    def _get_anywidget_state(self, include: set[str] | None) -> dict[str, int]:
        if include is not None and "value" not in include:
            return {}
        return {"value": self.value}


class NestedProbe(anywidget.AnyWidget):
    _esm = """
    function widgetRefs(value, refs = []) {
      if (typeof value === "string" && value.startsWith("anywidget:")) {
        refs.push(value);
      } else if (Array.isArray(value)) {
        for (const item of value) widgetRefs(item, refs);
      } else if (value && typeof value === "object") {
        for (const item of Object.values(value)) widgetRefs(item, refs);
      }
      return refs;
    }

    export default {
      async render({ model, el, host, signal, experimental }) {
        const count = document.createElement("output");
        count.dataset.testid = "nested-ref-count";
        const children = document.createElement("div");
        children.dataset.testid = "nested-children";

        const renderChildren = async () => {
          const refs = widgetRefs(model.get("children"));
          count.value = String(refs.length);
          children.replaceChildren();
          for (const ref of [...new Set(refs)]) {
            const root = document.createElement("div");
            root.dataset.modelRef = ref;
            children.append(root);
            const child = await host.getWidget(ref);
            await child.render({ el: root, signal });
          }
        };
        model.on("change:children", renderChildren);
        await renderChildren();

        const replace = document.createElement("button");
        replace.dataset.testid = "replace-nested";
        replace.textContent = "Replace nested children";
        replace.addEventListener("click", async () => {
          await experimental.invoke("replace_nested", { value: 20 }, { signal });
        });

        el.append(count, replace, children);
        return () => model.off("change:children", renderChildren);
      },
    };
    """

    children = traitlets.Any().tag(sync=True)

    @command
    def replace_nested(
        self,
        message: dict[str, Any],
        _buffers: list[bytes],
    ) -> tuple[dict[str, int], list[bytes]]:
        value = int(message["value"])
        protocol = ProtocolChild(value=value)
        self.children = {
            "groups": [
                protocol,
                {"pair": (ChildWidget(value=value + 1), protocol)},
            ]
        }
        return {"value": value}, []


def nested_state(widget: NestedProbe) -> dict[str, list[int]]:
    values: list[int] = []

    def collect(value: object) -> None:
        if isinstance(value, (ChildWidget, ProtocolChild)):
            values.append(value.value)
            return
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(widget.children)
    return {"values": values}


class BridgeProbe(anywidget.AnyWidget):
    _esm = """
    function bytes(view) {
      return Array.from(new Uint8Array(view.buffer, view.byteOffset, view.byteLength));
    }

    export default {
      async render({ model, el, signal, host, experimental }) {
        const binary = document.createElement("output");
        binary.dataset.testid = "binary-state";
        const updateBinary = () => {
          binary.value = `${bytes(model.get("payload")).join(",")} | size ${model.get("size")}`;
        };
        model.on("change:payload change:size", updateBinary);
        updateBinary();

        const changeBinary = document.createElement("button");
        changeBinary.dataset.testid = "change-binary";
        changeBinary.textContent = "Change binary";
        changeBinary.addEventListener("click", () => {
          model.set("payload", new Uint8Array([9, 8, 7, 6]));
          model.save_changes();
        });

        const command = document.createElement("button");
        command.dataset.testid = "invoke-command";
        command.textContent = "Invoke command";
        const commandResult = document.createElement("output");
        commandResult.dataset.testid = "command-result";
        command.addEventListener("click", async () => {
          const input = new DataView(new Uint8Array([1, 2, 3]).buffer);
          const [result, buffers] = await experimental.invoke(
            "reverse",
            { text: "adapter" },
            { buffers: [input], signal },
          );
          commandResult.value = `${result.text} | ${bytes(buffers[0]).join(",")}`;
        });

        const childRoot = document.createElement("div");
        childRoot.dataset.testid = "child-root";
        const renderChild = async () => {
          childRoot.replaceChildren();
          const child = await host.getWidget(model.get("child"));
          await child.render({ el: childRoot, signal });
        };
        model.on("change:child", renderChild);
        await renderChild();

        const replaceChild = document.createElement("button");
        replaceChild.dataset.testid = "replace-child";
        replaceChild.textContent = "Replace child";
        replaceChild.addEventListener("click", async () => {
          await experimental.invoke("replace_child", { value: 11 }, { signal });
        });

        el.append(
          binary,
          changeBinary,
          command,
          commandResult,
          replaceChild,
          childRoot,
        );
        return () => {
          model.off("change:payload change:size", updateBinary);
          model.off("change:child", renderChild);
        };
      },
    };
    """

    payload = traitlets.Bytes(bytes([0, 127, 255])).tag(sync=True)
    size = traitlets.Int(3).tag(sync=True)
    child = anywidget.WidgetTrait().tag(sync=True)

    @traitlets.observe("payload")
    def _update_size(self, change: traitlets.Bunch) -> None:
        self.size = len(change.new)

    @command
    def reverse(
        self,
        message: dict[str, Any],
        buffers: list[bytes],
    ) -> tuple[dict[str, str], list[bytes]]:
        return {"text": str(message["text"])[::-1]}, [
            buffer[::-1] for buffer in buffers
        ]

    @command
    def replace_child(
        self,
        message: dict[str, Any],
        _buffers: list[bytes],
    ) -> tuple[dict[str, int], list[bytes]]:
        value = int(message["value"])
        self.child = ChildWidget(value=value)
        return {"value": value}, []


class HotReloadProbe(anywidget.AnyWidget):
    _esm = """
    export default {
      render({ el, experimental, signal }) {
        const label = document.createElement("output");
        label.dataset.testid = "hot-label";
        label.value = "first generation";

        const css = document.createElement("button");
        css.dataset.testid = "change-css";
        css.textContent = "Change CSS";
        css.addEventListener("click", async () => {
          await experimental.invoke("set_blue_css", {}, { signal });
        });

        const esm = document.createElement("button");
        esm.dataset.testid = "change-esm";
        esm.textContent = "Change ESM";
        esm.addEventListener("click", async () => {
          try {
            await experimental.invoke("replace_esm", {}, { signal });
          } catch (error) {
            if (!signal.aborted) throw error;
          }
        });

        el.append(label, css, esm);
      },
    };
    """
    _css = '[data-testid="hot-label"] { color: rgb(220, 38, 38); }'

    @command
    def set_blue_css(
        self,
        _message: object,
        _buffers: list[bytes],
    ) -> tuple[dict[str, str], list[bytes]]:
        self._css = '[data-testid="hot-label"] { color: rgb(37, 99, 235); }'
        return {"color": "blue"}, []

    @command
    def replace_esm(
        self,
        _message: object,
        _buffers: list[bytes],
    ) -> tuple[dict[str, bool], list[bytes]]:
        source = """
        export default {
          render({ el }) {
            const label = document.createElement("output");
            label.dataset.testid = "hot-label";
            label.value = "second generation";
            el.append(label);
          },
        };
        """
        self._esm = source
        return {"reloaded": True}, []


class ValidationProbe(anywidget.AnyWidget):
    value = traitlets.Int(1).tag(sync=True)
    _esm = """
    export default {
      render({ model, el, signal }) {
        const output = document.createElement("output");
        const draw = () => { output.value = String(model.get("value")); };
        model.on("change:value", draw);
        draw();
        const button = document.createElement("button");
        button.textContent = "Send invalid value";
        button.addEventListener("click", () => {
          model.set("value", "invalid");
          model.save_changes();
        }, { signal });
        el.append(output, button);
        return () => model.off("change:value", draw);
      }
    };
    """


class LargeStateProbe(anywidget.AnyWidget):
    payload = traitlets.Bytes(bytes(8 * 1024 * 1024 - 1) + b"\xff").tag(sync=True)
    payload_size = traitlets.Int(8 * 1024 * 1024).tag(sync=True)
    payload_checksum = traitlets.Int(255).tag(sync=True)
    rows = traitlets.List().tag(sync=True)
    row_count = traitlets.Int(0).tag(sync=True)
    last_label = traitlets.Unicode("").tag(sync=True)

    _esm = """
    export default {
      render({ model, el, signal }) {
        const binary = document.createElement("output");
        binary.dataset.testid = "large-binary";
        const rows = document.createElement("output");
        rows.dataset.testid = "large-json";
        const draw = () => {
          const bytes = model.get("payload");
          binary.value = `${bytes.byteLength} | ${bytes.getUint8(0)} | ${bytes.getUint8(bytes.byteLength - 1)}`;
          const records = model.get("rows");
          rows.value = `${records.length} | ${records.at(-1)?.label ?? ""}`;
        };
        model.on("change:payload change:rows", draw);
        draw();
        const upload = document.createElement("button");
        upload.textContent = "Upload binary";
        upload.addEventListener("click", () => {
          const bytes = new Uint8Array(8 * 1024 * 1024).fill(7);
          bytes[0] = 11;
          bytes[bytes.length - 1] = 13;
          model.set("payload", new DataView(bytes.buffer));
          model.save_changes();
        }, { signal });
        const records = document.createElement("button");
        records.textContent = "Upload records";
        records.addEventListener("click", () => {
          model.set("rows", Array.from({ length: 40000 }, (_, index) => ({
            index, label: `row ${index} λ`,
          })));
          model.save_changes();
        }, { signal });
        el.append(binary, upload, rows, records);
        return () => model.off("change:payload change:rows", draw);
      }
    };
    """

    @traitlets.observe("payload")
    def _update_payload(self, change: traitlets.Bunch) -> None:
        self.payload_size = len(change.new)
        self.payload_checksum = sum(change.new)

    @traitlets.observe("rows")
    def _update_rows(self, change: traitlets.Bunch) -> None:
        self.row_count = len(change.new)
        self.last_label = change.new[-1]["label"] if change.new else ""


STARTUP_MODULE = """
export default {
  async initialize({ model, experimental, signal }) {
    if (model.get("stage") < 41) {
      await experimental.invoke("advance", {}, { signal });
    }
  },
  render({ model, el, experimental, signal }) {
    const events = [];
    const output = document.createElement("output");
    output.dataset.testid = "startup-progress";
    const draw = () => {
      output.value = `Stage ${model.get("stage")}, ${events.length} events`;
    };
    const collect = (message) => { events.push(message.index); draw(); };
    model.on("msg:custom", collect);
    draw();
    const live = document.createElement("button");
    live.textContent = "Resume live events";
    live.addEventListener("click", async () => {
      model.off("msg:custom", collect);
      await experimental.invoke("emit_events", { count: 250 }, { signal });
      model.on("msg:custom", collect);
      await experimental.invoke("emit_events", { count: 1 }, { signal });
    }, { signal });
    el.append(output, live);
    return () => model.off("msg:custom", collect);
  }
};
"""


class StartupProbe(anywidget.AnyWidget):
    stage = traitlets.Int(0).tag(sync=True)
    _esm = STARTUP_MODULE + "\nexport const phase = 0;"

    @command
    def advance(self, _message: object, _buffers: list[bytes]):
        if self.stage == 0:
            for index in range(250):
                self.send({"index": index})
        self.stage += 1
        self._esm = STARTUP_MODULE + f"\nexport const phase = {self.stage % 2};"
        return {}, []

    @command
    def emit_events(self, message: dict[str, Any], _buffers: list[bytes]):
        for index in range(message["count"]):
            self.send({"index": index})
        return {}, []


class ProjectionProbe(anywidget.AnyWidget):
    payload = traitlets.Dict().tag(sync=True)
    _esm = """
    export default {
      render({ model, el }) {
        const output = document.createElement("output");
        const draw = () => {
          const payload = model.get("payload");
          let value = payload.nested;
          let depth = 0;
          while (Array.isArray(value)) { depth++; value = value[0]; }
          output.value = `${payload.values.length} records; ${payload.text.length} characters; ${depth} levels; ${value}`;
        };
        model.on("change:payload", draw);
        draw();
        const update = document.createElement("button");
        update.textContent = "Update deep value";
        update.onclick = () => {
          let nested = "updated";
          for (let index = 0; index < 300; index++) nested = [nested];
          model.set("payload", { ...model.get("payload"), nested });
          model.save_changes();
        };
        el.append(output, update);
        return () => model.off("change:payload", draw);
      }
    };
    """


def create_server() -> AnyWidgetMCP:
    server = AnyWidgetMCP(
        "AnyWidget browser bridge fixture",
        csp={"scriptDirectives": ["'wasm-unsafe-eval'"]},
        cors_origins=[
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://localhost:8082",
            "http://127.0.0.1:8082",
        ],
    )
    server.widget(create_anywidget)

    @server.widget(title="Browser bridge probe")
    def bridge_probe() -> BridgeProbe:
        """Open the browser integration probe."""
        return BridgeProbe(child=ChildWidget())

    server.widget(
        HotReloadProbe,
        name="hot_reload_probe",
        title="Hot reload probe",
        state=None,
    )

    @server.widget(name="large_asset_probe", title="Large asset probe", state=None)
    def large_asset_probe() -> LargeAssetProbe:
        """Open two models that share one three-megabyte ESM source."""
        return LargeAssetProbe(
            first=LargeAssetLeaf(label="first large source"),
            second=LargeAssetLeaf(label="second large source"),
        )

    @server.widget(
        name="nested_probe",
        title="Nested composition probe",
        state=nested_state,
    )
    def nested_probe() -> NestedProbe:
        """Open recursive AnyWidget composition with a protocol child."""
        protocol = ProtocolChild(value=3)
        return NestedProbe(
            children={
                "groups": [
                    protocol,
                    {"pair": (ChildWidget(value=7), protocol)},
                ]
            }
        )

    server.widget(ValidationProbe, name="validation_probe")
    server.widget(
        LargeStateProbe,
        name="large_state_probe",
        state=("payload_size", "payload_checksum", "row_count", "last_label"),
    )

    @server.widget(name="widget_group", state="value")
    def widget_group() -> list[ChildWidget]:
        return [ChildWidget(value=2), ChildWidget(value=5)]

    server.widget(StartupProbe, name="startup_probe", state="stage")

    @server.widget(name="projection_probe", state="payload")
    def projection_probe() -> ProjectionProbe:
        nested: Any = "complete"
        for _ in range(300):
            nested = cast(Any, [nested])
        return ProjectionProbe(
            payload={
                "values": list(range(200)),
                "text": "λ" * 1500,
                "nested": nested,
                "k" * 500: "full key",
            }
        )

    return server


async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ready")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8766, type=int)
    arguments = parser.parse_args(argv)
    server = create_server()
    try:
        app = server.streamable_http_app()
        app.routes.append(Route("/health", health))
        uvicorn.run(app, host=arguments.host, port=arguments.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
