from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from anywidget import AnyWidget, WidgetTrait
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
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
