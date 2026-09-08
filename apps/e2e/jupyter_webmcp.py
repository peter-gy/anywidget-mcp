from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import nbformat


CELLS = [
    '''import anywidget
import traitlets as t
from IPython.display import display
from anywidget_mcp import webmcp


counters = []


class Counter(anywidget.AnyWidget):
    value = t.Int(1, min=0).tag(sync=True)
    doubled = t.Int(2, read_only=True).tag(sync=True)
    secret = t.Unicode("private notebook note").tag(sync=True)
    _esm = """
    export default {
      render({ model, el, signal }) {
        const state = document.createElement("output");
        state.setAttribute("aria-label", "Counter state");
        const draw = () => {
          state.value = `${model.get("value")} / ${model.get("doubled")}`;
        };
        model.on("change:value change:doubled", draw);
        draw();
        const button = document.createElement("button");
        button.textContent = "Increment";
        button.addEventListener("click", () => {
          model.set("value", model.get("value") + 1);
          model.save_changes();
        }, { signal });
        el.append(state, button);
        return () => model.off("change:value change:doubled", draw);
      }
    };
    """

    def __init__(self, start: int = 1):
        super().__init__(value=start)
        counters.append(self)

    @t.observe("value")
    def double(self, change):
        self.set_trait("doubled", change.new * 2)

    @t.validate("value")
    def validate_value(self, proposal):
        if proposal.value == 13:
            raise t.TraitError("Counter value 13 is reserved")
        return proposal.value


def create_counter(start: int = 4) -> Counter:
    """Create a counter at the requested value."""
    return Counter(start=start)


async def async_counter(start: int = 6) -> Counter:
    """Create a counter asynchronously."""
    return Counter(start=start)


existing = Counter(start=2)
excluded = Counter(start=20)
display(existing)
display(excluded)
''',
    """tools = webmcp.enable(
    widgets={
        Counter: {"private": {"secret"}},
        create_counter: {},
        async_counter: {},
        existing: {"name": "existing", "read_only": True},
        excluded: False,
    },
    discover=True,
)
tools
""",
    """future = Counter(start=3)
future
""",
    """webmcp.enable(
    widgets={future: {"read_only": True}, existing: False},
    discover=False,
)
""",
    """webmcp.disable()
print("WebMCP disabled, existing widget open:", existing.comm is not None)
""",
    """tools.close()
print("WebMCP closed, existing widget open:", existing.comm is not None)
print("Created widgets closed:", sum(widget.comm is None for widget in counters))
""",
    """webmcp.enable(widgets=[create_counter], discover=False)
""",
]


def main() -> None:
    with TemporaryDirectory(prefix="anywidget-mcp-jupyter-") as directory:
        root = Path(directory)
        for name in ("config", "runtime", "data"):
            path = root / name
            path.mkdir()
            os.environ[f"JUPYTER_{name.upper()}_DIR"] = str(path)
        from ipykernel.kernelspec import get_kernel_dict
        from jupyterlab.labapp import LabApp

        kernel_dir = root / "data" / "kernels" / "python3"
        kernel_dir.mkdir(parents=True)
        (kernel_dir / "kernel.json").write_text(json.dumps(get_kernel_dict()))
        notebook = nbformat.v4.new_notebook(
            cells=[
                nbformat.v4.new_code_cell(
                    source, metadata={"jupyter": {"source_hidden": True}}
                )
                for source in CELLS
            ],
            metadata={
                "kernelspec": {
                    "display_name": "Python 3",
                    "language": "python",
                    "name": "python3",
                },
            },
        )
        nbformat.write(notebook, root / "webmcp.ipynb")
        LabApp.launch_instance(
            argv=[
                "--no-browser",
                "--expose-app-in-browser",
                "--ServerApp.ip=127.0.0.1",
                "--ServerApp.port=8793",
                "--ServerApp.port_retries=0",
                "--LanguageServerManager.autodetect=False",
                f"--LabApp.workspaces_dir={root / 'workspaces'}",
                f"--LabApp.user_settings_dir={root / 'settings'}",
                f"--ServerApp.root_dir={root}",
                "--IdentityProvider.token=anywidget-mcp-e2e",
            ]
        )


if __name__ == "__main__":
    main()
