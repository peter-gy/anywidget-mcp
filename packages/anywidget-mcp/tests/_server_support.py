from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from anywidget import AnyWidget, WidgetTrait
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent
from traitlets import Int, observe, validate

from anywidget_mcp import AnyWidgetMCP


def leaf_error_messages(error: BaseException) -> list[str]:
    if isinstance(error, BaseExceptionGroup):
        return [
            message
            for nested in error.exceptions
            for message in leaf_error_messages(nested)
        ]
    return [str(error)]


def bootstrap_id(result: CallToolResult) -> str:
    prefix = "urn:anywidget-mcp:bootstrap:"
    markers = [
        content.text
        for content in result.content
        if isinstance(content, TextContent) and content.text.startswith(prefix)
    ]
    assert len(markers) == 1
    value = markers[0].removeprefix(prefix)
    assert re.fullmatch(r"[0-9a-f]{32}", value)
    return value


def state_id(result: CallToolResult) -> str:
    structured = result.structuredContent
    assert structured is not None
    value = structured.get("state_id")
    assert isinstance(value, str)
    assert re.fullmatch(r"[0-9a-f]{32}", value)
    return value


async def bootstrap_runtime(
    client: ClientSession,
    result: CallToolResult,
    *,
    operation_id: str = "bootstrap",
) -> dict[str, Any]:
    bootstrap = await client.call_tool(
        "anywidget_bootstrap",
        {
            "bootstrap_id": bootstrap_id(result),
            "operation_id": operation_id,
        },
    )
    assert bootstrap.isError is False
    assert bootstrap.meta is not None
    runtime = bootstrap.meta["anywidget"]
    assert isinstance(runtime, dict)
    return runtime


class CounterWidget(AnyWidget):
    _esm = "export default { render() {} }"

    value = Int(0).tag(sync=True)
    doubled = Int(0).tag(sync=True)

    @observe("value")
    def _update_doubled(self, change: dict[str, Any]) -> None:
        self.doubled = change["new"] * 2


class ParentWidget(AnyWidget):
    _esm = "export default { render() {} }"

    child = WidgetTrait().tag(sync=True)


class ValidatedCounterWidget(AnyWidget):
    _esm = "export default { render() {} }"

    value = Int(0).tag(sync=True)

    @validate("value")
    def _clamp_value(self, proposal: dict[str, Any]) -> int:
        return min(10, max(0, proposal["value"]))


@asynccontextmanager
async def connected(server: AnyWidgetMCP) -> AsyncGenerator[ClientSession, None]:
    async with create_connected_server_and_client_session(
        server, raise_exceptions=True
    ) as client:
        yield client
