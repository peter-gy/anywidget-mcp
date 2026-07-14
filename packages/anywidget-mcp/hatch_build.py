from __future__ import annotations

from pathlib import Path
from typing import cast

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Require the browser artifact shipped by the Python distribution."""

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if version == "editable":
            return

        if self.target_name == "sdist":
            raw_force_include = build_data.get("force_include")
            if isinstance(raw_force_include, dict):
                force_include = cast(dict[str, str], raw_force_include)
                for source, target in tuple(force_include.items()):
                    if target == ".gitignore":
                        del force_include[source]

        artifact = Path(self.root, "src", "anywidget_mcp", "static", "index.html")
        if not artifact.is_file():
            raise RuntimeError(
                "Build @anywidget-mcp/python before packaging: "
                "src/anywidget_mcp/static/index.html is missing"
            )
