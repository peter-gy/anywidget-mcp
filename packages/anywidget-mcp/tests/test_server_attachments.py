from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, cast

import pytest
from anywidget import AnyWidget
from mcp.client import Client
from mcp.types import CallToolResult, JSONRPCRequest, JSONRPCResponse, TextContent
from traitlets import Any as TraitAny
from traitlets import Bytes, Unicode, observe

import anywidget_mcp._attachments as attachment_module
from anywidget_mcp.server import AnyWidgetMCP

from ._server_support import (
    bootstrap_id,
    bootstrap_runtime,
    connected,
    decode_delivery,
    read_blob,
    state_id,
    upload_blob,
)


class DataWidget(AnyWidget):
    _esm = "export default {render(){}}"
    data = Bytes().tag(sync=True)
    label = Unicode().tag(sync=True)


def wire_size(result: Any) -> int:
    return len(result.model_dump_json(by_alias=True).encode())


@pytest.mark.anyio
async def test_large_binary_and_json_bootstrap_have_bounded_wire_frames() -> None:
    data = bytes(range(256)) * (8 * 1024 * 1024 // 256)
    label = "π" * 80_000
    widget = DataWidget(data=data, label=label)
    server = AnyWidgetMCP("large")
    server.widget(lambda: widget, name="data")
    async with Client(server) as client:
        launch = await client.call_tool("data", {})
        bootstrap = await client.call_tool(
            "anywidget_bootstrap",
            {"bootstrap_id": bootstrap_id(launch), "operation_id": "launch"},
        )
        assert wire_size(launch) < 128 * 1024
        assert wire_size(bootstrap) < 128 * 1024
        assert bootstrap.meta is not None
        delivery = bootstrap.meta["anywidget"]
        assert set(delivery) == {"protocolVersion", "instanceId", "payloadRef"}
        decoded = await decode_delivery(client, bootstrap)
        assert decoded.meta is not None
        payload = decoded.meta["anywidget"]
        model = payload["models"][payload["rootModelId"]]
        assert model["state"]["label"] == label
        ref = model["buffers"][0]
        assert ref["byteLength"] == 8 * 1024 * 1024
        assert await read_blob(client, payload["instanceId"], ref) == data
        chunk = await client.call_tool(
            "anywidget_read",
            {
                "instance_id": payload["instanceId"],
                "blob_id": ref["id"],
                "offset": 65536,
            },
        )
        assert wire_size(chunk) < 128 * 1024
        source = await read_blob(
            client, payload["instanceId"], model["sourceRefs"]["_esm"]
        )
        assert source.decode() == widget._esm
        assert launch.structured_content is not None
        assert launch.structured_content["state"]["data"] == {
            "type": "binary",
            "bytes": len(data),
        }


@pytest.mark.anyio
async def test_uploaded_binary_and_large_json_comm_apply_once_and_replay() -> None:
    updates: list[int] = []

    class Observed(DataWidget):
        @observe("data")
        def changed(self, change: Any) -> None:
            updates.append(len(change.new))

    widget = Observed()
    server = AnyWidgetMCP("upload")
    server.widget(lambda: widget, name="data")
    async with connected(server) as client:
        launch = await client.call_tool("data", {})
        runtime = await bootstrap_runtime(client, launch)
        instance = runtime["instanceId"]
        data = b"uploaded" * (1024 * 1024)
        binary = await upload_blob(client, instance, data, 1)
        label = "long label" * 20_000
        body = {
            "data": {
                "method": "update",
                "state": {"label": label},
                "buffer_paths": [["data"]],
            },
            "buffers": [binary],
        }
        request = await upload_blob(client, instance, json.dumps(body).encode(), 1)
        arguments = {
            "instance_id": instance,
            "model_id": runtime["rootModelId"],
            "operation_id": 1,
            "payload_ref": request,
        }
        first = await client.call_tool("anywidget_comm", arguments)
        repeated = await client.call_tool("anywidget_comm", arguments)
        assert not first.is_error
        assert repeated.meta == first.meta
        assert widget.data == data
        assert widget.label == label
        assert updates == [len(data)]
        assert first.meta is not None
        echo = first.meta["anywidget"]["messages"][0]
        assert await read_blob(client, instance, echo["buffers"][0]) == data
        state = await client.call_tool(
            "anywidget_state", {"state_id": state_id(launch)}
        )
        assert state.structured_content is not None
        assert state.structured_content["state"]["data"] == {
            "type": "binary",
            "bytes": len(data),
        }


@pytest.mark.anyio
async def test_upload_chunks_retry_exactly_and_reject_invalid_ranges_and_digests() -> (
    None
):
    server = AnyWidgetMCP("chunks")
    server.widget(DataWidget)
    async with connected(server) as client:
        runtime = await bootstrap_runtime(
            client, await client.call_tool("data_widget", {})
        )
        instance = runtime["instanceId"]
        data = b"a" * 65536 + b"end"
        identifier = "sha256:" + hashlib.sha256(data).hexdigest()
        common = {
            "instance_id": instance,
            "blob_id": identifier,
            "byte_length": len(data),
            "operation_id": 1,
        }

        async def write(offset: int, content: bytes):
            return await client.call_tool(
                "anywidget_write",
                {
                    **common,
                    "offset": offset,
                    "data": base64.b64encode(content).decode(),
                },
            )

        assert (await write(1, b"a")).is_error
        first = await write(0, data[:65536])
        assert not first.is_error
        assert (await write(0, data[:65536])).meta == first.meta
        assert (await write(0, b"wrong")).is_error
        assert (await write(65537, b"x")).is_error
        assert (await write(0, b"x" * 65537)).is_error
        final = await write(65536, b"end")
        assert not final.is_error
        assert (await write(65536, b"end")).meta == final.meta
        assert (await write(0, data[:65536])).meta == final.meta
        ref = {"id": identifier, "byteLength": len(data)}
        assert await read_blob(client, instance, ref) == data
        for offset in (-1, len(data) + 1):
            assert (
                await client.call_tool(
                    "anywidget_read",
                    {"instance_id": instance, "blob_id": identifier, "offset": offset},
                )
            ).is_error
        eof = await client.call_tool(
            "anywidget_read",
            {"instance_id": instance, "blob_id": identifier, "offset": len(data)},
        )
        assert eof.meta is not None
        assert eof.meta["anywidget"]["data"] == ""
        empty = await upload_blob(client, instance, b"", 2)
        assert await read_blob(client, instance, empty) == b""
        bad = await client.call_tool(
            "anywidget_write",
            {
                "instance_id": instance,
                "blob_id": "sha256:" + "0" * 64,
                "operation_id": 3,
                "byte_length": 3,
                "offset": 0,
                "data": base64.b64encode(b"bad").decode(),
            },
        )
        assert bad.is_error
        assert (
            await client.call_tool(
                "anywidget_read",
                {"instance_id": instance, "blob_id": "sha256:" + "0" * 64},
            )
        ).is_error


@pytest.mark.anyio
async def test_comm_groups_hold_many_buffers_and_release_independently() -> None:
    widget = DataWidget()
    received: list[list[bytes]] = []
    widget.on_msg(lambda _widget, _content, buffers: received.append(buffers))
    server = AnyWidgetMCP("grouped")
    server.widget(lambda: widget, name="data")
    async with connected(server) as client:
        runtime = await bootstrap_runtime(client, await client.call_tool("data", {}))
        instance = runtime["instanceId"]
        expected = [f"buffer {index}".encode() for index in range(12)]
        first_refs = [
            await upload_blob(client, instance, value, 1) for value in expected
        ]
        shared = await upload_blob(client, instance, expected[0], 2)
        separate = await upload_blob(client, instance, b"second operation", 2)
        first = await client.comm(
            {
                "instance_id": instance,
                "model_id": runtime["rootModelId"],
                "operation_id": 1,
                "data": {"method": "custom", "content": {}},
                "buffers": first_refs,
            },
        )
        assert not first.is_error
        assert received == [expected]
        assert await read_blob(client, instance, shared) == expected[0]
        assert await read_blob(client, instance, separate) == b"second operation"
        released = await client.call_tool(
            "anywidget_read", {"instance_id": instance, "blob_id": first_refs[-1]["id"]}
        )
        assert released.is_error
        second = await client.comm(
            {
                "instance_id": instance,
                "model_id": runtime["rootModelId"],
                "operation_id": 2,
                "data": {"method": "custom", "content": {}},
                "buffers": [shared, separate],
            },
        )
        assert not second.is_error
        assert received == [expected, [expected[0], b"second operation"]]


@pytest.mark.anyio
async def test_comm_error_replay_stays_within_transport_budget() -> None:
    widget = DataWidget()
    server = AnyWidgetMCP("bounded-error")
    server.widget(lambda: widget, name="data")
    attempts: list[bool] = []

    def fail(_message: Any) -> None:
        attempts.append(True)
        raise ValueError("error detail " * 20_000)

    async with connected(server) as client:
        runtime = await bootstrap_runtime(client, await client.call_tool("data", {}))
        assert widget.comm is not None
        widget.comm.on_msg(fail)
        args = {
            "instance_id": runtime["instanceId"],
            "model_id": runtime["rootModelId"],
            "operation_id": 1,
            "data": {"method": "request_state"},
        }
        args = await client.prepare_comm(args)
        first = await client.raw.call_tool("anywidget_comm", args)
        replay = await client.raw.call_tool("anywidget_comm", args)
        assert first.is_error and replay.is_error
        assert first.content == replay.content
        assert wire_size(first) < 128 * 1024
        assert attempts == [True]


@pytest.mark.anyio
async def test_disposal_retries_attachment_close_and_attempts_every_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams: list[Any] = []
    fail_close = [False]
    original = attachment_module.Attachments._file

    class RetryFile:
        def __init__(self) -> None:
            self.file = original()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.file, name)

        def close(self) -> None:
            if fail_close[0]:
                fail_close[0] = False
                raise OSError("temporary close failure")
            self.file.close()

    def tracked_file() -> Any:
        stream = RetryFile()
        streams.append(stream)
        return stream

    monkeypatch.setattr(
        attachment_module.Attachments, "_file", staticmethod(tracked_file)
    )
    server = AnyWidgetMCP("retry-cleanup")
    server.widget(DataWidget)
    async with connected(server) as client:
        runtime = await bootstrap_runtime(
            client, await client.call_tool("data_widget", {})
        )
        await upload_blob(client, runtime["instanceId"], b"staged data", 1)
        fail_close[0] = True
        disposed = await client.call_tool(
            "anywidget_dispose", {"session_id": runtime["instanceId"]}
        )
        assert not disposed.is_error
        assert all(stream.closed for stream in streams)


@pytest.mark.anyio
async def test_oversized_removal_acknowledgments_preserve_pending_delivery() -> None:
    widget = DataWidget()
    server = AnyWidgetMCP("ack-budget")
    server.widget(lambda: widget, name="data")
    async with connected(server) as client:
        runtime = await bootstrap_runtime(client, await client.call_tool("data", {}))
        widget.label = "pending"
        invalid = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": runtime["instanceId"],
                "operation_id": 1,
                "acknowledged_model_ids": [f"model-{index}" for index in range(10_000)],
            },
        )
        assert invalid.is_error
        assert wire_size(invalid) < 128 * 1024
        valid = await client.call_tool(
            "anywidget_poll", {"instance_id": runtime["instanceId"], "operation_id": 1}
        )
        assert not valid.is_error
        assert valid.meta is not None
        assert any(
            message["data"].get("state") == {"label": "pending"}
            for message in valid.meta["anywidget"]["messages"]
        )


@pytest.mark.anyio
async def test_unacknowledged_comm_history_remains_replayable() -> None:
    widget = DataWidget()
    calls = [0]
    widget.on_msg(lambda *_args: calls.__setitem__(0, calls[0] + 1))
    server = AnyWidgetMCP("history")
    server.widget(lambda: widget, name="data", state=None)
    async with connected(server) as client:
        runtime = await bootstrap_runtime(client, await client.call_tool("data", {}))
        common = {
            "instance_id": runtime["instanceId"],
            "model_id": runtime["rootModelId"],
            "data": {"method": "custom", "content": {}},
        }
        first = None
        for sequence in range(1, 161):
            result = await client.comm({**common, "operation_id": sequence})
            assert not result.is_error
            if sequence == 1:
                first = result
        assert first is not None
        replay = await client.comm({**common, "operation_id": 1})
        assert replay == first
        assert calls == [160]
        acknowledged = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": runtime["instanceId"],
                "operation_id": 161,
                "acknowledged_operation_id": 160,
            },
        )
        assert not acknowledged.is_error
        assert (await client.comm({**common, "operation_id": 1})).is_error
        assert (await client.comm({**common, "operation_id": 160})).is_error
        assert calls == [160]


@pytest.mark.anyio
async def test_poll_history_and_acknowledgment_watermark() -> None:
    widget = DataWidget()
    server = AnyWidgetMCP("poll-history")
    server.widget(lambda: widget, name="data")
    async with connected(server) as client:
        runtime = await bootstrap_runtime(client, await client.call_tool("data", {}))
        instance = runtime["instanceId"]
        widget.label = "first"
        first = await client.call_tool(
            "anywidget_poll", {"instance_id": instance, "operation_id": 1}
        )
        widget.label = "second"
        second = await client.call_tool(
            "anywidget_poll", {"instance_id": instance, "operation_id": 2}
        )
        assert (
            await client.call_tool(
                "anywidget_poll", {"instance_id": instance, "operation_id": 1}
            )
        ) == first
        assert (
            await client.call_tool(
                "anywidget_poll", {"instance_id": instance, "operation_id": 2}
            )
        ) == second
        acknowledged = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": instance,
                "operation_id": 3,
                "acknowledged_operation_id": 2,
            },
        )
        assert not acknowledged.is_error
        assert (
            await client.call_tool(
                "anywidget_poll", {"instance_id": instance, "operation_id": 1}
            )
        ).is_error
        assert (
            await client.call_tool(
                "anywidget_poll",
                {
                    "instance_id": instance,
                    "operation_id": 4,
                    "acknowledged_operation_id": 1,
                },
            )
        ).is_error is False
        assert (
            await client.call_tool(
                "anywidget_poll",
                {
                    "instance_id": instance,
                    "operation_id": 3,
                    "acknowledged_operation_id": 0,
                },
            )
        ) == acknowledged
        ahead = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": instance,
                "operation_id": 5,
                "acknowledged_operation_id": 5,
            },
        )
        assert ahead.is_error
        assert not (
            await client.call_tool(
                "anywidget_poll",
                {
                    "instance_id": instance,
                    "operation_id": 5,
                    "acknowledged_operation_id": 4,
                },
            )
        ).is_error


@pytest.mark.anyio
async def test_applied_cancellation_preserves_reply_until_acknowledgment() -> None:
    widget = DataWidget()
    data = b"x" * (2 * 1024 * 1024)
    detail = "response" * 20_000
    calls = [0]

    def respond(_widget: Any, _content: Any, _buffers: Any) -> None:
        calls[0] += 1
        widget.send({"detail": detail}, buffers=[data])

    widget.on_msg(respond)
    server = AnyWidgetMCP("reply-lifetime")
    server.widget(lambda: widget, name="data", state=None)
    async with connected(server) as client:
        runtime = await bootstrap_runtime(client, await client.call_tool("data", {}))
        instance = runtime["instanceId"]
        args = {
            "instance_id": instance,
            "model_id": runtime["rootModelId"],
            "operation_id": 1,
            "data": {"method": "custom", "content": {}},
        }
        args = await client.prepare_comm(args)
        raw = await client.raw.call_tool("anywidget_comm", args)
        assert raw.meta is not None
        body_ref = raw.meta["anywidget"]["payloadRef"]
        response = await decode_delivery(client.raw, raw)
        assert response.meta is not None
        message = response.meta["anywidget"]["messages"][0]
        binary_ref = message["buffers"][0]
        assert message["data"]["content"] == {"detail": detail}
        cancelled = await client.call_tool(
            "anywidget_cancel", {"instance_id": instance, "operation_id": 1}
        )
        assert cancelled.meta is not None
        assert cancelled.meta["anywidget"] == {
            "protocolVersion": 3,
            "instanceId": instance,
            "operationId": 1,
            "retired": True,
        }
        assert (await client.raw.call_tool("anywidget_comm", args)) == raw
        assert await read_blob(client, instance, binary_ref) == data
        assert calls == [1]
        acknowledged = await client.call_tool(
            "anywidget_cancel",
            {
                "instance_id": instance,
                "operation_id": 1,
                "acknowledged_operation_id": 1,
            },
        )
        assert not acknowledged.is_error
        for ref in (body_ref, binary_ref):
            assert (
                await client.call_tool(
                    "anywidget_read", {"instance_id": instance, "blob_id": ref["id"]}
                )
            ).is_error
        assert (await client.call_tool("anywidget_comm", args)).is_error
        assert calls == [1]


@pytest.mark.anyio
async def test_cancel_and_successor_dispatch_preserve_future_uploads() -> None:
    server = AnyWidgetMCP("cancelled-upload")
    server.widget(DataWidget)
    async with connected(server) as client:
        runtime = await bootstrap_runtime(
            client, await client.call_tool("data_widget", {})
        )
        instance = runtime["instanceId"]
        old = await upload_blob(client, instance, b"cancelled", 1)
        skipped = await upload_blob(client, instance, b"skipped", 3)
        future = await upload_blob(client, instance, b"future", 5)
        for _ in range(2):
            assert not (
                await client.call_tool(
                    "anywidget_cancel", {"instance_id": instance, "operation_id": 1}
                )
            ).is_error
        assert (
            await client.call_tool(
                "anywidget_read", {"instance_id": instance, "blob_id": old["id"]}
            )
        ).is_error
        assert await read_blob(client, instance, skipped) == b"skipped"
        assert await read_blob(client, instance, future) == b"future"
        assert not (
            await client.call_tool(
                "anywidget_poll", {"instance_id": instance, "operation_id": 4}
            )
        ).is_error
        assert (
            await client.call_tool(
                "anywidget_read", {"instance_id": instance, "blob_id": skipped["id"]}
            )
        ).is_error
        assert await read_blob(client, instance, future) == b"future"
        applied = await client.comm(
            {
                "instance_id": instance,
                "model_id": runtime["rootModelId"],
                "operation_id": 5,
                "acknowledged_operation_id": 4,
                "data": {"method": "update", "state": {}, "buffer_paths": [["data"]]},
                "buffers": [future],
            },
        )
        assert not applied.is_error
        assert await read_blob(client, instance, future) == b"future"


@pytest.mark.anyio
async def test_many_large_uploads_close_with_operation_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams: list[Any] = []
    original = attachment_module.Attachments._file

    def tracked_file() -> Any:
        stream = original()
        streams.append(stream)
        return stream

    monkeypatch.setattr(
        attachment_module.Attachments, "_file", staticmethod(tracked_file)
    )
    server = AnyWidgetMCP("large-pending")
    server.widget(DataWidget)
    async with connected(server) as client:
        runtime = await bootstrap_runtime(
            client, await client.call_tool("data_widget", {})
        )
        instance = runtime["instanceId"]
        first_upload = len(streams)
        for sequence in range(1, 13):
            partial = await client.call_tool(
                "anywidget_write",
                {
                    "instance_id": instance,
                    "operation_id": sequence,
                    "blob_id": "sha256:"
                    + hashlib.sha256(str(sequence).encode()).hexdigest(),
                    "byte_length": 2 * 1024 * 1024 * 1024,
                    "offset": 0,
                    "data": "YQ==",
                },
            )
            assert not partial.is_error
            assert partial.meta is not None
            assert partial.meta["anywidget"]["received"] == 1
            assert partial.meta["anywidget"]["complete"] is False
        cancelled = await client.call_tool(
            "anywidget_cancel", {"instance_id": instance, "operation_id": 12}
        )
        assert not cancelled.is_error
        assert all(stream.closed for stream in streams[first_upload:])
        source = runtime["models"][runtime["rootModelId"]]["sourceRefs"]["_esm"]
        assert await read_blob(client, instance, source)


@pytest.mark.anyio
@pytest.mark.parametrize("acknowledgment", [-1, True, 1.5, 2**53])
async def test_acknowledgments_require_nonnegative_safe_integers(
    acknowledgment: Any,
) -> None:
    server = AnyWidgetMCP("ack-type")
    server.widget(DataWidget)
    async with connected(server) as client:
        runtime = await bootstrap_runtime(
            client, await client.call_tool("data_widget", {})
        )
        rejected = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": runtime["instanceId"],
                "operation_id": 1,
                "acknowledged_operation_id": acknowledgment,
            },
        )
        assert rejected.is_error
        assert not (
            await client.call_tool(
                "anywidget_poll",
                {"instance_id": runtime["instanceId"], "operation_id": 1},
            )
        ).is_error


@pytest.mark.anyio
@pytest.mark.parametrize("depth", [16, 300])
async def test_deep_state_survives_actual_mcp_json_encoding(depth: int) -> None:
    value: Any = "leaf"
    for _ in range(depth):
        value = cast(Any, [value])

    class DeepWidget(AnyWidget):
        _esm = "export default {render(){}}"
        nested = TraitAny().tag(sync=True)

    widget = DeepWidget(nested=value)
    state = {"value": value}
    serialized_state = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    server = AnyWidgetMCP("deep")
    server.widget(
        lambda: widget, name="deep", state=lambda root: {"value": root.nested}
    )

    def roundtrip(result: CallToolResult) -> dict[str, Any]:
        encoded = result.model_dump_json(by_alias=True)
        parsed = json.loads(encoded)
        response = JSONRPCResponse(jsonrpc="2.0", id=1, result=parsed)
        return json.loads(response.model_dump_json(by_alias=True))["result"]

    class EncodedClient:
        def __init__(self, raw: Client) -> None:
            self.raw = raw

        async def call_tool(
            self, name: str, arguments: dict[str, Any]
        ) -> CallToolResult:
            request = JSONRPCRequest(
                jsonrpc="2.0",
                id=1,
                method="tools/call",
                params={"name": name, "arguments": arguments},
            )
            received = JSONRPCRequest.model_validate_json(
                request.model_dump_json(by_alias=True)
            )
            assert received.params is not None
            result = await self.raw.call_tool(
                received.params["name"], received.params["arguments"]
            )
            response = JSONRPCResponse(
                jsonrpc="2.0", id=1, result=result.model_dump(by_alias=True)
            )
            decoded = JSONRPCResponse.model_validate_json(
                response.model_dump_json(by_alias=True)
            )
            return CallToolResult.model_validate(decoded.result)

    async with Client(server) as raw_client:
        client = EncodedClient(raw_client)
        launch = await client.call_tool("deep", {})
        launch_wire = roundtrip(launch)
        assert isinstance(launch.content[0], TextContent)
        assert serialized_state in launch.content[0].text
        handle = state_id(launch)
        bootstrap = await client.call_tool(
            "anywidget_bootstrap",
            {"bootstrap_id": bootstrap_id(launch), "operation_id": "claim"},
        )
        wire = roundtrip(bootstrap)
        delivery = wire["_meta"]["anywidget"]
        if depth == 300:
            assert "payloadRef" in delivery
            assert launch_wire["structuredContent"] == {
                "tool": "deep",
                "state_id": handle,
            }
        else:
            assert launch_wire["structuredContent"]["state"] == state
        decoded = await decode_delivery(client, bootstrap)
        assert decoded.meta is not None
        payload = decoded.meta["anywidget"]
        assert payload["models"][payload["rootModelId"]]["state"]["nested"] == value
        assert payload["context"]["state"] == state
        current = await client.call_tool("anywidget_state", {"state_id": handle})
        current_wire = roundtrip(current)
        assert isinstance(current.content[0], TextContent)
        assert serialized_state in current.content[0].text
        if depth == 300:
            assert current_wire["structuredContent"] == {
                "state_id": handle,
                "tool": "deep",
                "version": 1,
            }
        else:
            assert current_wire["structuredContent"]["state"] == state

        replacement: Any = "updated"
        for _ in range(depth):
            replacement = cast(Any, [replacement])
        body = json.dumps(
            {
                "data": {"method": "update", "state": {"nested": replacement}},
                "buffers": [],
            },
            separators=(",", ":"),
        ).encode()
        ref = await upload_blob(client, payload["instanceId"], body, 1)
        changed = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": payload["instanceId"],
                "model_id": payload["rootModelId"],
                "operation_id": 1,
                "payload_ref": ref,
            },
        )
        assert not changed.is_error
        assert widget.nested == replacement
        changed_payload = await decode_delivery(client, changed)
        assert changed_payload.meta is not None
        assert changed_payload.meta["anywidget"]["context"]["state"] == {
            "value": replacement
        }
        final = await client.call_tool("anywidget_state", {"state_id": handle})
        assert isinstance(final.content[0], TextContent)
        assert (
            json.dumps({"value": replacement}, separators=(",", ":"))
            in final.content[0].text
        )
