from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest
from anywidget import AnyWidget
from mcp.server.fastmcp import Context
from traitlets import Int
from wigglystuff import ColorPicker

from anywidget_mcp import cli
from anywidget_mcp.server import serve


class PositionalWidget(ColorPicker):
    def __init__(self, color: str, /) -> None:
        super().__init__(color=color)


class AnywidgetPoll(ColorPicker):
    pass


class MinimalWidget(AnyWidget):
    _esm = "export default { render() {} }"

    value = Int(0).tag(sync=True)


class UnsupportedInput:
    pass


def make_widget(value: int, label: str = "ready") -> MinimalWidget:
    """Create a minimal widget."""
    del label
    return MinimalWidget(value=value)


async def make_async_widget(value: int = 3) -> MinimalWidget:
    """Create a minimal widget asynchronously."""
    return MinimalWidget(value=value)


def make_context_widget(value: int, ctx: Context) -> MinimalWidget:
    """Create a widget with the active MCP request context."""
    del ctx
    return MinimalWidget(value=value)


def make_private_widget(_value: int) -> MinimalWidget:
    return MinimalWidget(value=_value)


def make_unsupported_widget(value: UnsupportedInput) -> MinimalWidget:
    del value
    return MinimalWidget()


@pytest.fixture
def target_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("widget_target_fixture")
    setattr(module, "MinimalWidget", MinimalWidget)
    setattr(module, "make_widget", make_widget)
    setattr(module, "make_async_widget", make_async_widget)
    setattr(module, "make_context_widget", make_context_widget)
    setattr(module, "make_private_widget", make_private_widget)
    setattr(module, "make_unsupported_widget", make_unsupported_widget)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


def test_root_help_lists_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])

    captured = capsys.readouterr()
    assert exit_info.value.code == 0
    assert "usage: anywidget-mcp" in captured.out
    assert "serve" in captured.out
    assert "inspect" in captured.out
    assert captured.err == ""


def test_serve_help_documents_the_import_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["serve", "--help"])

    captured = capsys.readouterr()
    assert exit_info.value.code == 0
    assert "MODULE:OBJECT" in captured.out
    assert "--port" in captured.out
    assert captured.err == ""


def test_inspect_help_documents_json_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["inspect", "--help"])

    captured = capsys.readouterr()
    assert exit_info.value.code == 0
    assert "MODULE:OBJECT" in captured.out
    assert "--json" in captured.out
    assert captured.err == ""


def test_inspect_json_reports_empty_schema_for_minimal_widget_class(
    target_module: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.main(["inspect", f"{target_module.__name__}:MinimalWidget", "--json"])

    captured = capsys.readouterr()
    expected = {
        "description": "",
        "inputSchema": {
            "properties": {},
            "title": "MinimalWidgetArguments",
            "type": "object",
        },
        "kind": "widget-class",
        "target": "widget_target_fixture:MinimalWidget",
        "title": "Minimal Widget",
        "toolName": "minimal_widget",
    }
    assert json.loads(captured.out) == expected
    assert captured.err == ""


@pytest.mark.parametrize(
    ("attribute", "kind", "tool_name", "properties", "required"),
    [
        (
            "make_widget",
            "factory",
            "make_widget",
            {"value", "label"},
            ["value"],
        ),
        (
            "make_async_widget",
            "factory",
            "make_async_widget",
            {"value"},
            None,
        ),
    ],
)
def test_inspect_json_reports_explicit_factory_signatures(
    attribute: str,
    kind: str,
    tool_name: str,
    properties: set[str],
    required: list[str] | None,
    target_module: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.main(["inspect", f"{target_module.__name__}:{attribute}", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["kind"] == kind
    assert payload["toolName"] == tool_name
    assert set(payload["inputSchema"]["properties"]) == properties
    if required:
        assert payload["inputSchema"]["required"] == required
    else:
        assert "required" not in payload["inputSchema"]
    assert payload["inputSchema"]["properties"]["value"]["type"] == "integer"
    if "label" in properties:
        assert payload["inputSchema"]["properties"]["label"]["default"] == "ready"
    assert captured.err == ""


def test_inspect_human_output_contains_the_registration_contract(
    target_module: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.main(["inspect", f"{target_module.__name__}:make_widget"])

    captured = capsys.readouterr()
    assert "Target: widget_target_fixture:make_widget" in captured.out
    assert "Kind: factory" in captured.out
    assert "Tool name: make_widget" in captured.out
    assert "Title: Make Widget" in captured.out
    assert '"value"' in captured.out
    assert captured.err == ""


def test_inspect_excludes_the_injected_context_parameter(
    target_module: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.main(["inspect", f"{target_module.__name__}:make_context_widget", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["kind"] == "factory"
    assert payload["inputSchema"]["required"] == ["value"]
    assert set(payload["inputSchema"]["properties"]) == {"value"}
    assert captured.err == ""


def test_cli_imports_widget_class_and_forwards_server_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeServer:
        def __init__(self, name: str, **options: object) -> None:
            events.append(("init", (name, options)))

        def widget(self, target: object) -> None:
            events.append(("widget", target))

        def run(self, *, transport: str) -> None:
            events.append(("run", transport))

    monkeypatch.setattr(cli, "AnyWidgetMCP", FakeServer)

    cli.main(
        [
            "serve",
            "wigglystuff:ColorPicker",
            "--host",
            "0.0.0.0",
            "--port",
            "8123",
            "--transport",
            "stdio",
            "--log-level",
            "DEBUG",
        ]
    )

    assert events == [
        (
            "init",
            (
                "ColorPicker MCP",
                {"host": "0.0.0.0", "port": 8123, "log_level": "DEBUG"},
            ),
        ),
        ("widget", ColorPicker),
        ("run", "stdio"),
    ]


@pytest.mark.parametrize("attribute", ["make_widget", "make_async_widget"])
def test_cli_imports_factories_for_server_registration(
    attribute: str,
    target_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: list[object] = []

    class FakeServer:
        def __init__(self, _name: str, **_options: object) -> None:
            pass

        def widget(self, target: object) -> None:
            registered.append(target)

        def run(self, *, transport: str) -> None:
            assert transport == "stdio"

    monkeypatch.setattr(cli, "AnyWidgetMCP", FakeServer)

    cli.main(
        [
            "serve",
            f"{target_module.__name__}:{attribute}",
            "--transport",
            "stdio",
        ]
    )

    assert registered == [getattr(target_module, attribute)]


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("ColorPicker", "Use module:object"),
        ("missing_widget_package:Widget", "Could not import module"),
        ("wigglystuff:MissingWidget", "has no target"),
        ("builtins:str", "expected an AnyWidget subclass"),
    ],
)
def test_cli_reports_invalid_targets_without_a_traceback(
    target: str,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["serve", target])

    captured = capsys.readouterr()
    assert exit_info.value.code == 2
    assert captured.out == ""
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_cli_rejects_a_widget_instance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = types.ModuleType("widget_target_fixture")
    picker = ColorPicker()
    setattr(module, "picker", picker)
    monkeypatch.setitem(sys.modules, module.__name__, module)

    try:
        with pytest.raises(SystemExit) as exit_info:
            cli.main(["serve", "widget_target_fixture:picker"])
    finally:
        picker.close()

    assert exit_info.value.code == 2
    assert "received a ColorPicker instance" in capsys.readouterr().err


def test_cli_validates_port_range(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["serve", "wigglystuff:ColorPicker", "--port", "70000"])

    assert exit_info.value.code == 2
    assert "port must be between 1 and 65535" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("widget_class", "message"),
    [
        (PositionalWidget, "Widget parameters must accept keyword arguments"),
        (AnywidgetPoll, "reserved for the AnyWidget app"),
    ],
)
@pytest.mark.parametrize("command", ["serve", "inspect"])
def test_cli_reports_widget_registration_errors_without_a_traceback(
    widget_class: type[ColorPicker],
    message: str,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = types.ModuleType("widget_registration_fixture")
    setattr(module, "Widget", widget_class)
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "widget_registration_fixture:Widget"])

    captured = capsys.readouterr()
    assert exit_info.value.code == 2
    assert captured.out == ""
    assert message in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("attribute", "detail"),
    [
        ("make_private_widget", "cannot start with '_'"),
        ("make_unsupported_widget", "Cannot generate a JsonSchema"),
    ],
)
@pytest.mark.parametrize("command", ["serve", "inspect"])
def test_cli_reports_schema_compilation_errors_without_a_traceback(
    attribute: str,
    detail: str,
    command: str,
    target_module: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, f"{target_module.__name__}:{attribute}"])

    captured = capsys.readouterr()
    assert exit_info.value.code == 2
    assert captured.out == ""
    assert f"Could not compile widget target '{attribute}'" in captured.err
    assert detail in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "runtime_error",
    [TypeError("runtime type failure"), ValueError("runtime value failure")],
)
def test_cli_preserves_runtime_failures(
    runtime_error: TypeError | ValueError,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeServer:
        def __init__(self, _name: str, **_options: object) -> None:
            pass

        def widget(self, _target: object) -> None:
            pass

        def run(self, *, transport: str) -> None:
            del transport
            raise runtime_error

    monkeypatch.setattr(cli, "AnyWidgetMCP", FakeServer)

    with pytest.raises(type(runtime_error), match=str(runtime_error)):
        cli.main(["serve", "wigglystuff:ColorPicker"])


def test_serve_registers_and_runs_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []

    class FakeServer:
        def __init__(self, server_name: str, **options: Any) -> None:
            events.append(("init", (server_name, options)))

        def widget(self, target: object, **options: Any) -> None:
            events.append(("widget", (target, options)))

        def run(self, *, transport: str) -> None:
            events.append(("run", transport))

    monkeypatch.setattr("anywidget_mcp.server.AnyWidgetMCP", FakeServer)

    serve(
        ColorPicker,
        host="0.0.0.0",
        port=8123,
        transport="stdio",
        state=("color",),
    )

    assert events == [
        (
            "init",
            (
                "ColorPicker MCP",
                {
                    "host": "0.0.0.0",
                    "port": 8123,
                    "log_level": "INFO",
                    "icons": None,
                },
            ),
        ),
        (
            "widget",
            (
                ColorPicker,
                {
                    "name": None,
                    "title": None,
                    "description": None,
                    "state": ("color",),
                    "annotations": None,
                    "icons": None,
                },
            ),
        ),
        ("run", "stdio"),
    ]


def test_serve_preserves_runtime_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeServer:
        def __init__(self, *_args: Any, **_options: Any) -> None:
            pass

        def widget(self, _target: object, **_options: Any) -> None:
            pass

        def run(self, *, transport: str) -> None:
            del transport
            raise RuntimeError("failed")

    monkeypatch.setattr("anywidget_mcp.server.AnyWidgetMCP", FakeServer)

    with pytest.raises(RuntimeError, match="failed"):
        serve(ColorPicker)
