from __future__ import annotations

import argparse
import threading
from collections.abc import Sequence
from typing import Any

import anywidget
import traitlets
from anywidget._descriptor import MimeBundleDescriptor
from anywidget.experimental import command

from anywidget_mcp import AnyWidgetMCP


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
          await experimental.invoke("schedule_esm_reload", {}, { signal });
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
    def schedule_esm_reload(
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
        timer = threading.Timer(0.05, setattr, args=(self, "_esm", source))
        timer.daemon = True
        timer.start()
        return {"scheduled": True}, []


def create_server(*, host: str, port: int) -> AnyWidgetMCP:
    server = AnyWidgetMCP(
        "AnyWidget browser bridge fixture",
        host=host,
        port=port,
        cors_origins=[
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://localhost:8082",
            "http://127.0.0.1:8082",
        ],
    )

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

    return server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8766, type=int)
    arguments = parser.parse_args(argv)
    server = create_server(host=arguments.host, port=arguments.port)
    try:
        server.run(transport="streamable-http")
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


if __name__ == "__main__":
    main()
