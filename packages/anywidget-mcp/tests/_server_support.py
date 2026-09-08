from __future__ import annotations

import re
import base64
import hashlib
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from exceptiongroup import BaseExceptionGroup
from anywidget import AnyWidget, WidgetTrait
from mcp.client import Client
from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from traitlets import Int, observe, validate


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
    structured = result.structured_content
    assert structured is not None
    value = structured.get("state_id")
    assert isinstance(value, str)
    assert re.fullmatch(r"[0-9a-f]{32}", value)
    return value


async def bootstrap_runtime(
    client: Client | ClientSession | WidgetClient,
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
    bootstrap = await decode_delivery(client, bootstrap)
    assert bootstrap.is_error is False
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
async def connected(server: MCPServer) -> AsyncGenerator[WidgetClient, None]:
    async with Client(server, raise_exceptions=True) as client:
        yield WidgetClient(client)


async def read_blob(client: Any, instance_id: str, ref: dict[str, Any]) -> bytes:
    chunks = bytearray()
    while len(chunks) < ref["byteLength"] or not chunks:
        result = await client.call_tool(
            "anywidget_read",
            {"instance_id": instance_id, "blob_id": ref["id"], "offset": len(chunks)},
        )
        assert not result.is_error, result.content
        payload = result.meta["anywidget"]
        assert payload["protocolVersion"] == 3
        assert payload["id"] == ref["id"]
        assert payload["byteLength"] == ref["byteLength"]
        assert payload["offset"] == len(chunks)
        data = base64.b64decode(payload["data"], validate=True)
        assert len(data) <= 65536
        chunks.extend(data)
        if not data:
            break
    assert len(chunks) == ref["byteLength"]
    assert "sha256:" + hashlib.sha256(chunks).hexdigest() == ref["id"]
    return bytes(chunks)


async def upload_blob(
    client: Any, instance_id: str, data: bytes, operation_id: int
) -> dict[str, Any]:
    ref = {"id": "sha256:" + hashlib.sha256(data).hexdigest(), "byteLength": len(data)}
    for offset in range(0, max(len(data), 1), 65536):
        chunk = data[offset : offset + 65536]
        result = await client.call_tool(
            "anywidget_write",
            {
                "instance_id": instance_id,
                "blob_id": ref["id"],
                "byte_length": len(data),
                "operation_id": operation_id,
                "offset": offset,
                "data": base64.b64encode(chunk).decode(),
            },
        )
        assert not result.is_error, result.content
        payload = result.meta["anywidget"]
        assert offset + len(chunk) <= payload["received"] <= len(data)
        assert payload["complete"] is (payload["received"] == len(data))
        if payload["complete"]:
            break
    return ref


async def decode_delivery(client: Any, result: CallToolResult) -> CallToolResult:
    if result.is_error or result.meta is None:
        return result
    delivery = result.meta.get("anywidget")
    if not isinstance(delivery, dict) or not (
        {"payload", "payloadRef"} & delivery.keys()
    ):
        return result
    assert delivery["protocolVersion"] == 3
    if "payloadRef" in delivery:
        assert "payload" not in delivery
        payload = json.loads(
            await read_blob(client, delivery["instanceId"], delivery["payloadRef"])
        )
    else:
        payload = delivery["payload"]
    decoded = result.model_copy(deep=True)
    assert decoded.meta is not None
    decoded.meta["anywidget"] = {
        "protocolVersion": delivery["protocolVersion"],
        **payload,
    }
    return decoded


class WidgetClient:
    def __init__(self, raw: Client | ClientSession) -> None:
        self.raw = raw
        self._prepared_comm: dict[tuple[str, int, str], dict[str, Any]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **options: Any
    ) -> CallToolResult:
        result = await self.raw.call_tool(name, arguments, **options)
        assert isinstance(result, CallToolResult)
        return await decode_delivery(self.raw, result)

    async def prepare_comm(self, arguments: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(
            {"data": arguments["data"], "buffers": arguments.get("buffers", [])},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        key = (
            arguments["instance_id"],
            arguments["operation_id"],
            hashlib.sha256(encoded).hexdigest(),
        )
        ref = self._prepared_comm.get(key)
        if ref is None:
            ref = await upload_blob(
                self.raw, arguments["instance_id"], encoded, arguments["operation_id"]
            )
            self._prepared_comm[key] = ref
        return {
            **{
                name: value
                for name, value in arguments.items()
                if name not in {"data", "buffers"}
            },
            "payload_ref": ref,
        }

    async def comm(self, arguments: dict[str, Any]) -> CallToolResult:
        return await self.call_tool(
            "anywidget_comm", await self.prepare_comm(arguments)
        )
