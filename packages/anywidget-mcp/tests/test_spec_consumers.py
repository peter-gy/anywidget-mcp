from __future__ import annotations

import asyncio
from typing import Any
from dataclasses import replace
from unittest.mock import patch

from anywidget import AnyWidget
import pytest
import traitlets as t

from anywidget_mcp import webmcp
from anywidget_mcp._spec import scan
from anywidget_mcp._mcp.targets import compile_mcp_target
from anywidget_mcp.server import AnyWidgetMCP

from ._server_support import connected


class PolicyCounter(AnyWidget):
    _esm = "export default { render() {} };"
    value = t.Int(0).tag(sync=True)
    doubled = t.Int(0, read_only=True).tag(sync=True)
    notes = t.Unicode("local note").tag(sync=True, webmcp=False)

    @t.observe("value")
    def _double(self, change: dict[str, Any]) -> None:
        self.set_trait("doubled", change["new"] * 2)


def create_counter(start: int = 2) -> PolicyCounter:
    """Create a counter at the requested value."""
    return PolicyCounter(value=start)


@pytest.mark.anyio
async def test_mcp_and_webmcp_consume_shared_facts_with_independent_exposure_policies() -> (
    None
):
    definition = scan(create_counter)
    assert definition.model is not None
    assert definition.model.default_state_names == ("doubled", "notes", "value")
    assert definition.model.trait("notes").metadata["webmcp"] is False

    server = AnyWidgetMCP("Counter tools")
    server.widget(create_counter)
    session = webmcp.enable(
        widgets={create_counter: {"read_only": {"value"}}}, discover=False
    )
    try:
        catalog = session.get_state()["_webmcp_catalog"][0]
        assert catalog["inputSchema"] == {
            "type": "object",
            "properties": {"start": {"type": "integer", "default": 2}},
            "required": [],
            "additionalProperties": False,
        }
        async with connected(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            schema = tools["create_counter"].input_schema
            assert schema["properties"]["start"]["type"] == "integer"
            assert schema["properties"]["start"]["default"] == 2
            assert "loading_message" in schema["properties"]
            mcp_result = await client.call_tool("create_counter", {"start": 3})
            assert mcp_result.structured_content is not None
            assert mcp_result.structured_content["state"] == {
                "value": 3,
                "doubled": 6,
                "notes": "local note",
            }

            delivered: asyncio.Future[dict[str, Any]] = (
                asyncio.get_running_loop().create_future()
            )

            def capture(data: dict[str, Any], **_: Any) -> None:
                if data.get("method") == "custom" and not delivered.done():
                    delivered.set_result(data["content"])

            assert session.comm is not None
            with patch.object(session.comm, "send", side_effect=capture):
                session._handle_custom_msg(
                    {
                        "kind": "anywidget-webmcp",
                        "id": "create",
                        "operation": "create",
                        "target": catalog["id"],
                        "arguments": {"start": 3},
                    },
                    [],
                )
                response = await asyncio.wait_for(delivered, 5)
            assert response["result"]["state"] == {"value": 3, "doubled": 6}
            assert set(response["result"]["tools"]) == {"read"}
    finally:
        session.close()


@pytest.mark.anyio
async def test_custom_source_inspection_preserves_provenance_and_supplies_creation() -> (
    None
):
    source = {"widget": "counter", "initial": 5}

    def inspector(declaration: object):
        definition = scan(create_counter)
        return replace(definition, source=declaration)

    definition = scan(source, inspector=inspector)
    assert definition.source is source
    assert definition.call is not None
    assert definition.call.invoke is create_counter
    server = AnyWidgetMCP("Declarative counter")
    server._register_widget(compile_mcp_target(definition))
    async with connected(server) as client:
        result = await client.call_tool("create_counter", {"start": source["initial"]})
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["state"] == {
        "value": 5,
        "doubled": 10,
        "notes": "local note",
    }
