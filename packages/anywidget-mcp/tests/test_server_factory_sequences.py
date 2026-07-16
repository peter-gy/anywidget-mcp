from __future__ import annotations

from collections import UserList
from collections.abc import AsyncGenerator, Generator, Iterator
from contextlib import asynccontextmanager, contextmanager

import anyio
import pytest
from mcp.types import TextContent

from anywidget_mcp import AnyWidgetMCP
from anywidget_mcp._state import DEFAULT_STATE
from anywidget_mcp.server import _FactoryOwner, _SessionRuntime

from ._server_support import CounterWidget, ParentWidget, connected


@pytest.mark.anyio
async def test_direct_and_async_factories_return_ordered_widget_sequences() -> None:
    server = AnyWidgetMCP("test")
    created: dict[str, tuple[CounterWidget, ...]] = {}

    @server.widget
    def direct() -> list[CounterWidget]:
        widgets = (CounterWidget(value=1), CounterWidget(value=2))
        created["direct"] = widgets
        return list(widgets)

    @server.widget
    async def awaited() -> tuple[CounterWidget, CounterWidget]:
        await anyio.sleep(0)
        widgets = (CounterWidget(value=3), CounterWidget(value=4))
        created["awaited"] = widgets
        return widgets

    async with connected(server) as client:
        direct_result = await client.call_tool("direct", {})
        awaited_result = await client.call_tool("awaited", {})

        for name, result, values in (
            ("direct", direct_result, (1, 2)),
            ("awaited", awaited_result, (3, 4)),
        ):
            assert result.isError is False
            assert result.structuredContent == {
                "tool": name,
                "state": {
                    "widgets": [
                        {"doubled": value * 2, "value": value} for value in values
                    ]
                },
            }
            assert result.meta is not None
            runtime = result.meta["anywidget"]
            roots = created[name]
            group = runtime["models"][runtime["rootModelId"]]
            assert group["state"]["_items"] == [
                f"anywidget:{widget.model_id}" for widget in roots
            ]
            disposed = await client.call_tool(
                "anywidget_dispose",
                {"instance_id": runtime["instanceId"]},
            )
            assert disposed.structuredContent == {"disposed": True}

    assert all(
        widget.comm is None for widgets in created.values() for widget in widgets
    )


@pytest.mark.anyio
async def test_managed_factories_keep_widget_sequences_alive_until_disposal() -> None:
    server = AnyWidgetMCP("test")
    events: list[str] = []

    class TrackedWidget(CounterWidget):
        def __init__(self, label: str, value: int) -> None:
            self.label = label
            super().__init__(value=value)

        def close(self) -> None:
            if self.comm is not None:
                events.append(f"{self.label} close")
            super().close()

    @server.widget
    @contextmanager
    def managed() -> Generator[list[TrackedWidget], None, None]:
        events.append("managed enter")
        try:
            yield [
                TrackedWidget("managed first", 1),
                TrackedWidget("managed second", 2),
            ]
        finally:
            events.append("managed exit")

    @server.widget
    @asynccontextmanager
    async def async_managed() -> AsyncGenerator[
        tuple[TrackedWidget, TrackedWidget], None
    ]:
        events.append("async enter")
        try:
            yield (
                TrackedWidget("async first", 3),
                TrackedWidget("async second", 4),
            )
        finally:
            await anyio.sleep(0)
            events.append("async exit")

    async with connected(server) as client:
        managed_result = await client.call_tool("managed", {})
        async_result = await client.call_tool("async_managed", {})
        assert managed_result.meta is not None
        assert async_result.meta is not None
        await client.call_tool(
            "anywidget_dispose",
            {"instance_id": managed_result.meta["anywidget"]["instanceId"]},
        )
        await client.call_tool(
            "anywidget_dispose",
            {"instance_id": async_result.meta["anywidget"]["instanceId"]},
        )

    assert events == [
        "managed enter",
        "async enter",
        "managed first close",
        "managed second close",
        "managed exit",
        "async first close",
        "async second close",
        "async exit",
    ]


@pytest.mark.anyio
async def test_factory_sequence_is_snapshotted_at_return() -> None:
    server = AnyWidgetMCP("test")
    first = CounterWidget(value=1)
    second = CounterWidget(value=2)
    returned = [first, second]

    @server.widget
    def counters() -> list[CounterWidget]:
        return returned

    async with connected(server) as client:
        launch = await client.call_tool("counters", {})
        assert launch.meta is not None
        runtime = launch.meta["anywidget"]
        first_model_id = first.model_id
        second_model_id = second.model_id
        third = CounterWidget(value=3)
        returned.append(third)

        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": runtime["instanceId"],
                "operation_id": "sequence-snapshot",
            },
        )
        await client.call_tool(
            "anywidget_dispose",
            {"instance_id": runtime["instanceId"]},
        )

    assert poll.isError is False
    assert set(runtime["models"]) == {
        runtime["rootModelId"],
        first_model_id,
        second_model_id,
    }
    assert first.comm is None
    assert second.comm is None
    assert third.comm is not None
    third.close()


@pytest.mark.anyio
async def test_empty_and_invalid_factory_sequences_report_the_result_boundary() -> None:
    server = AnyWidgetMCP("test")
    events: list[str] = []

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            if self.comm is not None:
                events.append("widget close")
            super().close()

    @server.widget
    def empty() -> list[CounterWidget]:
        return []

    @server.widget
    @contextmanager
    def invalid() -> Generator[list[object], None, None]:
        try:
            yield [TrackedWidget(), "not a widget"]
        finally:
            events.append("resource exit")

    async with connected(server) as client:
        empty_result = await client.call_tool("empty", {})
        invalid_result = await client.call_tool("invalid", {})

    assert empty_result.isError is True
    assert isinstance(empty_result.content[0], TextContent)
    assert (
        "Widget factory returned an empty widget sequence"
        in empty_result.content[0].text
    )
    assert invalid_result.isError is True
    assert isinstance(invalid_result.content[0], TextContent)
    assert (
        "Widget factory context manager yielded str at sequence index 1, "
        "expected AnyWidget"
    ) in invalid_result.content[0].text
    assert events == ["widget close", "resource exit"]


@pytest.mark.anyio
async def test_invalid_nested_sequence_closes_every_returned_widget() -> None:
    server = AnyWidgetMCP("test")
    closed: list[str] = []

    class TrackedWidget(CounterWidget):
        def __init__(self, label: str) -> None:
            self.label = label
            super().__init__()

        def close(self) -> None:
            if self.comm is not None:
                closed.append(self.label)
            super().close()

    @server.widget
    def invalid() -> list[object]:
        return [TrackedWidget("outer"), [TrackedWidget("nested")]]

    async with connected(server) as client:
        result = await client.call_tool("invalid", {})

    assert result.isError is True
    assert isinstance(result.content[0], TextContent)
    assert "list at sequence index 1, expected AnyWidget" in result.content[0].text
    assert closed == ["nested", "outer"]


@pytest.mark.anyio
async def test_invalid_user_sequences_close_discovered_widgets() -> None:
    server = AnyWidgetMCP("test")
    closed: list[str] = []

    class TrackedWidget(CounterWidget):
        def __init__(self, label: str) -> None:
            self.label = label
            super().__init__()

        def close(self) -> None:
            if self.comm is not None:
                closed.append(self.label)
            super().close()

    class InterruptedUserList(UserList[object]):
        def __iter__(self) -> Iterator[object]:
            yield self.data[0]
            raise RuntimeError("sequence iteration failed")

    @server.widget
    def cyclic() -> list[object]:
        nested: UserList[object] = UserList()
        nested.extend((nested, TrackedWidget("cyclic nested")))
        return [TrackedWidget("cyclic outer"), nested]

    @server.widget
    def interrupted() -> list[object]:
        nested = InterruptedUserList([TrackedWidget("interrupted nested")])
        return [TrackedWidget("interrupted outer"), nested]

    async with connected(server) as client:
        cyclic_result = await client.call_tool("cyclic", {})
        interrupted_result = await client.call_tool("interrupted", {})

    assert cyclic_result.isError is True
    assert interrupted_result.isError is True
    assert closed == [
        "cyclic nested",
        "cyclic outer",
        "interrupted nested",
        "interrupted outer",
    ]


@pytest.mark.anyio
async def test_owner_closes_a_cancelled_unclaimed_sequence_graph_child_first() -> None:
    events: list[str] = []

    class TrackedChild(CounterWidget):
        def close(self) -> None:
            if self.comm is not None:
                events.append("child close")
            super().close()

    class TrackedParent(ParentWidget):
        def close(self) -> None:
            if self.comm is not None:
                events.append("parent close")
            super().close()

    class TrackedSibling(CounterWidget):
        def close(self) -> None:
            if self.comm is not None:
                events.append("sibling close")
            super().close()

    child = TrackedChild()
    parent = TrackedParent(child=child)
    sibling = TrackedSibling()
    owner = _FactoryOwner()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(owner.run, lambda: [parent, sibling], {})
        output = await owner.wait_ready()
        assert output.state_roots == (parent, sibling)
        owner.request_close("cancelled before session creation")
        await owner.wait_closed()

    assert events == ["child close", "parent close", "sibling close"]
    assert child.comm is None
    assert parent.comm is None
    assert sibling.comm is None


@pytest.mark.anyio
async def test_unclaimed_sequence_cleanup_preserves_a_reused_live_widget() -> None:
    live = CounterWidget(value=1)
    fresh_closed = anyio.Event()

    class FreshWidget(CounterWidget):
        def close(self) -> None:
            super().close()
            fresh_closed.set()

    fresh = FreshWidget(value=2)
    owner = _FactoryOwner()

    async with anyio.create_task_group() as task_group:
        runtime = _SessionRuntime(
            task_group,
            session_idle_timeout=30,
            app_uri="ui://test/widget.html",
        )
        launch = await runtime.open(
            lambda: live,
            {},
            DEFAULT_STATE,
            tool_name="live",
            tool_title="Live",
        )
        assert launch.meta is not None

        task_group.start_soon(owner.run, lambda: [live, fresh], {})
        await owner.wait_ready()
        owner.request_close("cancelled before session creation")
        await owner.wait_closed()

        assert live.comm is not None
        assert fresh_closed.is_set()
        await runtime.aclose()
        task_group.cancel_scope.cancel()

    assert live.comm is None
    assert fresh.comm is None
