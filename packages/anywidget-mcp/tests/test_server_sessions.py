from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest
from mcp.types import TextContent

import anywidget_mcp.server as server_module
from anywidget_mcp import AnyWidgetMCP
from anywidget_mcp._state import DEFAULT_STATE
from anywidget_mcp.server import _SessionRuntime

from ._server_support import CounterWidget, ParentWidget, connected


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
async def test_session_use_refreshes_the_idle_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr(
        server_module,
        "time",
        SimpleNamespace(monotonic=lambda: now),
    )

    async with anyio.create_task_group() as task_group:
        runtime = _SessionRuntime(
            task_group,
            session_idle_timeout=1,
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
        lease = runtime._sessions[instance_id]
        assert lease.deadline == 1.0

        now = 0.4
        with runtime.use(instance_id):
            assert lease.deadline == 1.4
        assert lease.deadline == 1.4

        now = 0.8
        with runtime.use(instance_id):
            pass
        assert lease.deadline == 1.8
        await runtime.aclose()


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
