from __future__ import annotations

import ast
import importlib.util
from collections.abc import Generator
from pathlib import Path

import anywidget_mcp


def imports(path: Path, root: Path) -> Generator[str, None, None]:
    parts = path.relative_to(root).with_suffix("").parts
    module = ".".join(("anywidget_mcp", *parts))
    package = (
        module.removesuffix(".__init__")
        if path.name == "__init__.py"
        else module.rpartition(".")[0]
    )
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported = "." * node.level + (node.module or "")
            resolved = (
                importlib.util.resolve_name(imported, package)
                if node.level
                else imported
            )
            if node.module is None:
                yield from (f"{resolved}.{alias.name}" for alias in node.names)
            else:
                yield resolved
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
            and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
            in {"__import__", "import_module"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield node.args[0].value


def test_shared_specification_depends_on_source_libraries_and_its_own_ports() -> None:
    root = Path(anywidget_mcp.__file__).parent
    failures = []
    for path in (root / "_spec").rglob("*.py"):
        for imported in imports(path, root):
            if imported.startswith("anywidget_mcp") and not (
                imported == "anywidget_mcp._spec"
                or imported.startswith("anywidget_mcp._spec.")
            ):
                failures.append(f"{path.name}: {imported}")
            if imported.partition(".")[0] in {"mcp", "pydantic", "starlette", "anyio"}:
                failures.append(f"{path.name}: {imported}")
    assert failures == []


def test_spec_records_and_ports_are_independent_of_widget_libraries() -> None:
    root = Path(anywidget_mcp.__file__).parent
    for name in ("types.py", "ports.py"):
        imported = tuple(imports(root / "_spec" / name, root))
        assert all(
            module.partition(".")[0] not in {"anywidget", "ipywidgets", "traitlets"}
            for module in imported
        )


def test_live_model_binding_depends_inward_on_the_shared_specification() -> None:
    root = Path(anywidget_mcp.__file__).parent
    internal = [
        module
        for module in imports(root / "_models.py", root)
        if module.startswith("anywidget_mcp.")
    ]
    assert internal
    assert all(
        module == "anywidget_mcp._spec" or module.startswith("anywidget_mcp._spec.")
        for module in internal
    )
