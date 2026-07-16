from __future__ import annotations

import anyio
import pytest
from anyio.lowlevel import checkpoint
from mcp.types import TextContent

import anywidget_mcp.server as server_module
from anywidget_mcp import AnyWidgetMCP
from anywidget_mcp._state import DEFAULT_STATE
from anywidget_mcp.server import _SessionRuntime

from ._server_support import CounterWidget, ParentWidget, connected


@pytest.mark.anyio
async def test_runtime_owner_exit_waits_for_an_active_borrower() -> None:
    server = AnyWidgetMCP("test")
    owner_connected = anyio.Event()
    borrower_connected = anyio.Event()
    owner_exit_started = anyio.Event()
    owner_finished = anyio.Event()
    check_borrower = anyio.Event()
    borrower_checked = anyio.Event()
    release_borrower = anyio.Event()
    results: list[bool] = []

    @server.widget
    def counter(value: int) -> CounterWidget:
        return CounterWidget(value=value)

    async def owner_client() -> None:
        async with connected(server):
            owner_connected.set()
            await borrower_connected.wait()
            owner_exit_started.set()
        owner_finished.set()

    async def borrower_client() -> None:
        await owner_connected.wait()
        async with connected(server) as client:
            first = await client.call_tool("counter", {"value": 1})
            results.append(first.isError is False)
            borrower_connected.set()
            await check_borrower.wait()
            second = await client.call_tool("counter", {"value": 2})
            results.append(second.isError is False)
            borrower_checked.set()
            await release_borrower.wait()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(owner_client)
        task_group.start_soon(borrower_client)
        await owner_exit_started.wait()
        await checkpoint()
        assert not owner_finished.is_set()

        check_borrower.set()
        await borrower_checked.wait()
        assert results == [True, True]
        assert not owner_finished.is_set()
        release_borrower.set()

    assert owner_finished.is_set()


@pytest.mark.anyio
async def test_new_client_waits_for_the_closing_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = AnyWidgetMCP("test")
    owner_active = anyio.Event()
    release_owner = anyio.Event()
    close_started = anyio.Event()
    release_close = anyio.Event()
    entrant_entered = anyio.Event()
    entrant_tools: set[str] = set()
    original_aclose = server_module._SessionRuntime.aclose
    close_calls = 0

    async def controlled_aclose(runtime: _SessionRuntime) -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            close_started.set()
            await release_close.wait()
        await original_aclose(runtime)

    monkeypatch.setattr(server_module._SessionRuntime, "aclose", controlled_aclose)

    async def owner() -> None:
        async with connected(server) as client:
            await client.list_tools()
            owner_active.set()
            await release_owner.wait()

    async def entrant() -> None:
        async with connected(server) as client:
            entrant_tools.update(
                tool.name for tool in (await client.list_tools()).tools
            )
            entrant_entered.set()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(owner)
        await owner_active.wait()
        release_owner.set()
        await close_started.wait()
        task_group.start_soon(entrant)
        await checkpoint()
        assert not entrant_entered.is_set()

        release_close.set()
        await entrant_entered.wait()

    assert "anywidget_poll" in entrant_tools


@pytest.mark.anyio
async def test_dispose_releases_widget_session() -> None:
    server = AnyWidgetMCP("test")

    @server.widget
    def counter() -> CounterWidget:
        return CounterWidget()

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        instance_id = launch.meta["anywidget"]["instanceId"]
        first = await client.call_tool(
            "anywidget_dispose", {"instance_id": instance_id}
        )
        second = await client.call_tool(
            "anywidget_dispose", {"instance_id": instance_id}
        )
        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": instance_id,
                "operation_id": "poll-after-dispose",
            },
        )

    assert first.structuredContent == {"disposed": True}
    assert second.structuredContent == {"disposed": False}
    assert poll.isError is True
    assert isinstance(poll.content[0], TextContent)
    assert "Unknown widget session" in poll.content[0].text


@pytest.mark.anyio
async def test_idle_session_expires_when_an_app_never_claims_it() -> None:
    closed = anyio.Event()

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            super().close()
            closed.set()

    server = AnyWidgetMCP("test", session_idle_timeout=0.05)

    @server.widget
    def counter() -> TrackedWidget:
        return TrackedWidget()

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        with anyio.fail_after(1):
            await closed.wait()
        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": launch.meta["anywidget"]["instanceId"],
                "operation_id": "poll-expired-session",
            },
        )

    assert poll.isError is True
    assert isinstance(poll.content[0], TextContent)
    assert "Unknown widget session" in poll.content[0].text


@pytest.mark.anyio
async def test_aclose_closes_multiple_live_sessions() -> None:
    server = AnyWidgetMCP("test", session_idle_timeout=1.0)
    created: list[CounterWidget] = []

    @server.widget
    def counter() -> CounterWidget:
        widget = CounterWidget()
        created.append(widget)
        return widget

    async with connected(server) as client:
        first = await client.call_tool("counter", {})
        second = await client.call_tool("counter", {})

        assert first.isError is False
        assert second.isError is False
        assert all(widget.comm is not None for widget in created)
        await server.aclose()

    assert all(widget.comm is None for widget in created)


@pytest.mark.anyio
async def test_active_session_call_is_not_expired() -> None:
    active = anyio.Event()
    release = anyio.Event()

    async with anyio.create_task_group() as task_group:
        runtime = _SessionRuntime(
            task_group,
            session_idle_timeout=0.05,
            app_uri="ui://test/widget.html",
        )
        launch = await runtime.open(
            CounterWidget,
            {},
            DEFAULT_STATE,
            tool_name="counter",
            tool_title="Counter",
        )
        assert launch.meta is not None
        instance_id = launch.meta["anywidget"]["instanceId"]

        async def hold_call() -> None:
            with runtime.use(instance_id):
                active.set()
                await release.wait()

        task_group.start_soon(hold_call)
        await active.wait()
        await anyio.sleep(0.1)

        with runtime.use(instance_id):
            pass

        release.set()
        await runtime.aclose()


@pytest.mark.anyio
async def test_idle_cleanup_continues_when_one_widget_close_fails() -> None:
    closed = anyio.Event()

    class BrokenCloseWidget(CounterWidget):
        def close(self) -> None:
            fail = getattr(self, "_fail_close", True)
            self._fail_close = False
            super().close()
            if fail:
                raise RuntimeError("close failed")

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            super().close()
            closed.set()

    server = AnyWidgetMCP("test", session_idle_timeout=0.05)

    @server.widget
    def broken() -> BrokenCloseWidget:
        return BrokenCloseWidget()

    @server.widget
    def tracked() -> TrackedWidget:
        return TrackedWidget()

    async with connected(server) as client:
        first = await client.call_tool("broken", {})
        second = await client.call_tool("tracked", {})

        assert first.isError is False
        assert second.isError is False
        with anyio.fail_after(1):
            await closed.wait()


@pytest.mark.anyio
async def test_reused_root_is_rejected_without_closing_the_live_session() -> None:
    server = AnyWidgetMCP("test")
    widget = CounterWidget()

    @server.widget
    def counter() -> CounterWidget:
        return widget

    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]
        reused = await client.call_tool("counter", {})
        comm = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": payload["instanceId"],
                "model_id": payload["rootModelId"],
                "operation_id": "reused-root-live-session",
                "data": {"method": "request_state"},
            },
        )

    assert reused.isError is True
    assert isinstance(reused.content[0], TextContent)
    assert "Return a fresh root" in reused.content[0].text
    assert comm.isError is False


@pytest.mark.anyio
async def test_reused_child_is_rejected_without_closing_the_live_session() -> None:
    server = AnyWidgetMCP("test")
    child = CounterWidget()
    roots: list[ParentWidget] = []

    @server.widget
    def parent() -> ParentWidget:
        root = ParentWidget(child=child)
        roots.append(root)
        return root

    async with connected(server) as client:
        launch = await client.call_tool("parent", {})
        assert launch.meta is not None
        payload = launch.meta["anywidget"]
        reused = await client.call_tool("parent", {})
        comm = await client.call_tool(
            "anywidget_comm",
            {
                "instance_id": payload["instanceId"],
                "model_id": child.model_id,
                "operation_id": "reused-child-live-session",
                "data": {"method": "request_state"},
            },
        )

        assert roots[1].comm is None
        assert reused.isError is True
        assert isinstance(reused.content[0], TextContent)
        assert "fresh nested widgets" in reused.content[0].text
        assert comm.isError is False
