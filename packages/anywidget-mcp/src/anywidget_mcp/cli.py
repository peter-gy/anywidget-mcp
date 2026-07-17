"""Import, inspect, and serve AnyWidget targets from the command line."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .server import AnyWidgetMCP, WidgetTargetDescription, describe_widget_target


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "inspect":
        try:
            target = _resolve_target(arguments.target)
            description = describe_widget_target(target)
        except (TypeError, ValueError) as error:
            parser.error(str(error))
        _print_inspection(arguments.target, description, as_json=arguments.json)
        return

    try:
        targets = _prepare_targets(arguments.targets)
    except (TypeError, ValueError) as error:
        parser.error(str(error))

    server_name = (
        f"{_target_name(targets[0])} MCP" if len(targets) == 1 else "AnyWidget MCP"
    )
    server = AnyWidgetMCP(
        server_name,
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
    )
    try:
        for target in targets:
            server.widget(target)
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    try:
        server.run(
            transport=arguments.transport,
        )
    except KeyboardInterrupt:
        return


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anywidget-mcp",
        description="Expose AnyWidget classes and factories as MCP App tools.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve_command = commands.add_parser(
        "serve",
        help="import and serve widget targets",
        description=(
            "Import one or more MODULE:OBJECT targets and serve each as an "
            "MCP App tool."
        ),
    )
    serve_command.add_argument(
        "targets",
        nargs="+",
        metavar="MODULE:OBJECT",
        help=("AnyWidget class or factory to serve. Repeat to expose multiple tools."),
    )
    serve_command.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP bind address (default: %(default)s)",
    )
    serve_command.add_argument(
        "--port",
        default=8000,
        type=_port,
        help="HTTP port (default: %(default)s)",
    )
    serve_command.add_argument(
        "--transport",
        choices=["streamable-http", "stdio"],
        default="streamable-http",
        help="MCP transport (default: %(default)s)",
    )
    serve_command.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="server log level (default: %(default)s)",
    )

    inspect_command = commands.add_parser(
        "inspect",
        help="inspect a widget target",
        description="Inspect the MCP tool contract for MODULE:OBJECT.",
    )
    inspect_command.add_argument("target", metavar="MODULE:OBJECT")
    inspect_command.add_argument(
        "--json",
        action="store_true",
        help="write stable JSON to stdout",
    )
    return parser


def _resolve_target(target: str) -> Any:
    """Import a ``MODULE:OBJECT`` target and resolve its dotted attribute path."""

    _prioritize_working_directory()
    module_name, separator, attribute_path = target.partition(":")
    if not separator or not module_name or not attribute_path:
        raise ValueError(f"Invalid widget target {target!r}. Use module:object.")

    try:
        value: Any = importlib.import_module(module_name)
    except Exception as error:
        raise ValueError(f"Could not import module {module_name!r}: {error}") from error

    for attribute in attribute_path.split("."):
        if not attribute:
            raise ValueError(f"Invalid widget target {target!r}. Use module:object.")
        try:
            value = getattr(value, attribute)
        except AttributeError as error:
            raise ValueError(
                f"Module {module_name!r} has no target {attribute_path!r}."
            ) from error
    return value


def _prepare_targets(targets: Sequence[str]) -> list[Any]:
    """Resolve targets and reject model-facing tool name collisions."""

    prepared: list[Any] = []
    tool_sources: dict[str, str] = {}

    for source in targets:
        try:
            target = _resolve_target(source)
            description = describe_widget_target(target)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Widget target {source!r} is invalid: {error}") from error
        previous_source = tool_sources.get(description.tool_name)
        if previous_source is not None:
            raise ValueError(
                f"Targets {previous_source!r} and {source!r} both resolve to MCP "
                f"tool {description.tool_name!r}. Use "
                "AnyWidgetMCP.widget(..., name=...) to assign explicit names."
            )
        tool_sources[description.tool_name] = source
        prepared.append(target)

    return prepared


def _prioritize_working_directory() -> None:
    """Place the working directory first on ``sys.path`` for local imports."""

    working_directory = str(Path.cwd())
    if sys.path and sys.path[0] == working_directory:
        return
    try:
        sys.path.remove(working_directory)
    except ValueError:
        pass
    sys.path.insert(0, working_directory)


def _print_inspection(
    target: str,
    description: WidgetTargetDescription,
    *,
    as_json: bool,
) -> None:
    payload = {
        "description": description.description,
        "inputSchema": description.input_schema,
        "kind": description.kind,
        "target": target,
        "title": description.title,
        "toolName": description.tool_name,
    }
    if as_json:
        print(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        )
        return

    print(f"Target: {payload['target']}")
    print(f"Kind: {payload['kind']}")
    print(f"Tool name: {payload['toolName']}")
    print(f"Title: {payload['title']}")
    print(f"Description: {payload['description'] or ''}")
    print("Input schema:")
    print(
        json.dumps(payload["inputSchema"], ensure_ascii=False, indent=2, sort_keys=True)
    )


def _target_name(target: object) -> str:
    return str(getattr(target, "__name__", type(target).__name__))


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port
