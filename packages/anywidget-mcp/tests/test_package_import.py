from __future__ import annotations

import subprocess
import sys
import textwrap


def test_base_package_creates_widgets_with_server_dependencies_unavailable() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import importlib.abc
                import sys

                class BaseEnvironment(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname.partition('.')[0] in {
                            'mcp', 'starlette', 'pydantic', 'anyio'
                        }:
                            raise ModuleNotFoundError(fullname, name=fullname)

                sys.meta_path.insert(0, BaseEnvironment())
                from anywidget_mcp import StateProjection, create_anywidget, webmcp

                assert not webmcp.is_enabled()
                webmcp.enable()
                assert webmcp.is_enabled()

                widget = create_anywidget(
                    'import anywidget\\n'
                    'import traitlets\\n'
                    'class Counter(anywidget.AnyWidget):\\n'
                    '    value = traitlets.Int(3).tag(sync=True)'
                )
                try:
                    projection = StateProjection(
                        lambda widget: {'value': widget.value}, watch='value'
                    )
                    assert projection.project(widget) == {'value': 3}
                    widget.value = 5
                    assert projection.project(widget) == {'value': 5}
                finally:
                    webmcp.disable()
                    assert not webmcp.is_enabled()
                    widget.close()
                """
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
