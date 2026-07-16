from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from typing import Any

import anyio
import pytest
from anyio.lowlevel import checkpoint
from mcp.server.fastmcp import Context
from mcp.types import TextContent

import anywidget_mcp.server as server_module
from anywidget_mcp import AnyWidgetMCP, StateProjection
from anywidget_mcp._state import DEFAULT_STATE
from anywidget_mcp.server import _FactoryOwner, _SessionRuntime

from ._server_support import CounterWidget, connected, leaf_error_messages


@pytest.mark.anyio
async def test_context_is_injected_into_direct_and_managed_factories() -> None:
    server = AnyWidgetMCP("test")
    contexts: list[Context[Any, Any, Any]] = []
    progress_events: list[tuple[float, float | None, str | None]] = []

    @server.widget
    def direct(value: int, ctx: Context) -> CounterWidget:
        contexts.append(ctx)
        return CounterWidget(value=value)

    @server.widget
    @contextmanager
    def managed(value: int, ctx: Context) -> Generator[CounterWidget]:
        contexts.append(ctx)
        yield CounterWidget(value=value)

    @server.widget
    @asynccontextmanager
    async def async_managed(
        value: int,
        ctx: Context,
    ) -> AsyncGenerator[CounterWidget, None]:
        contexts.append(ctx)
        await ctx.report_progress(1, 2, "Prepared widget")
        yield CounterWidget(value=value)

    async def record_progress(
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        progress_events.append((progress, total, message))

    async with connected(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        direct_result = await client.call_tool("direct", {"value": 1})
        managed_result = await client.call_tool("managed", {"value": 2})
        async_result = await client.call_tool(
            "async_managed",
            {"value": 3},
            progress_callback=record_progress,
        )

    assert all(
        "ctx" not in tools[name].inputSchema["properties"]
        for name in ("direct", "managed", "async_managed")
    )
    assert all(isinstance(ctx, Context) for ctx in contexts)
    assert direct_result.structuredContent == {
        "tool": "direct",
        "state": {"doubled": 2, "value": 1},
    }
    assert managed_result.structuredContent == {
        "tool": "managed",
        "state": {"doubled": 4, "value": 2},
    }
    assert async_result.structuredContent == {
        "tool": "async_managed",
        "state": {"doubled": 6, "value": 3},
    }
    assert progress_events == [(1.0, 2.0, "Prepared widget")]


@pytest.mark.anyio
async def test_managed_factory_cleanup_runs_on_owner_task_after_widget_close() -> None:
    server = AnyWidgetMCP("test")
    events: list[str] = []
    owner_tasks: list[int] = []

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            if self.comm is not None:
                events.append("widget close")
            super().close()

    @server.widget
    @asynccontextmanager
    async def managed() -> AsyncGenerator[TrackedWidget, None]:
        owner_tasks.append(anyio.get_current_task().id)
        events.append("resource enter")
        try:
            yield TrackedWidget()
        finally:
            await anyio.sleep(0)
            events.append("resource exit")
            owner_tasks.append(anyio.get_current_task().id)

    async with connected(server) as client:
        launch = await client.call_tool("managed", {})
        assert launch.meta is not None
        disposed = await client.call_tool(
            "anywidget_dispose",
            {"instance_id": launch.meta["anywidget"]["instanceId"]},
        )

    assert disposed.structuredContent == {"disposed": True}
    assert events == ["resource enter", "widget close", "resource exit"]
    assert owner_tasks[0] == owner_tasks[1]


@pytest.mark.anyio
async def test_async_factory_can_return_an_awaited_context_manager() -> None:
    server = AnyWidgetMCP("test")
    events: list[str] = []

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            events.append("widget close")
            super().close()

    @asynccontextmanager
    async def resource() -> AsyncGenerator[TrackedWidget, None]:
        events.append("resource enter")
        try:
            yield TrackedWidget()
        finally:
            events.append("resource exit")

    @server.widget
    async def managed() -> AbstractAsyncContextManager[TrackedWidget]:
        await anyio.sleep(0)
        return resource()

    async with connected(server) as client:
        launch = await client.call_tool("managed", {})
        assert launch.meta is not None
        disposed = await client.call_tool(
            "anywidget_dispose",
            {"instance_id": launch.meta["anywidget"]["instanceId"]},
        )

    assert disposed.structuredContent == {"disposed": True}
    assert events == ["resource enter", "widget close", "resource exit"]


@pytest.mark.anyio
async def test_request_cancellation_closes_managed_factory_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            events.append("widget close")
            super().close()

    def project(widget: TrackedWidget) -> dict[str, int]:
        return {"value": widget.value}

    @contextmanager
    def managed() -> Generator[TrackedWidget]:
        widget = TrackedWidget()
        events.append("resource enter")
        try:
            yield widget
        finally:
            events.append("resource exit")

    async with anyio.create_task_group() as task_group:
        runtime = _SessionRuntime(
            task_group,
            session_idle_timeout=30,
            app_uri="ui://test/widget.html",
        )

        with anyio.CancelScope() as request_scope:

            async def cancel_before_session_commit() -> None:
                request_scope.cancel()
                await checkpoint()

            monkeypatch.setattr(
                server_module,
                "checkpoint",
                cancel_before_session_commit,
            )
            await runtime.open(
                managed,
                {},
                StateProjection(project, watch="value"),
                tool_name="managed",
                tool_title="Managed",
            )
        assert request_scope.cancel_called
        await runtime.aclose()
        task_group.cancel_scope.cancel()

    assert events == ["resource enter", "widget close", "resource exit"]


@pytest.mark.anyio
async def test_managed_factory_exits_on_idle_expiry_and_server_shutdown() -> None:
    server = AnyWidgetMCP("test", session_idle_timeout=0.05)
    events: list[str] = []
    idle_exited = anyio.Event()
    shutdown_exited = anyio.Event()

    class TrackedWidget(CounterWidget):
        def __init__(self, label: str) -> None:
            super().__init__()
            self.label = label

        def close(self) -> None:
            if self.comm is not None:
                events.append(f"{self.label} close")
            super().close()

    @asynccontextmanager
    async def resource(label: str) -> AsyncGenerator[TrackedWidget, None]:
        events.append(f"{label} enter")
        try:
            yield TrackedWidget(label)
        finally:
            events.append(f"{label} exit")
            (idle_exited if label == "idle" else shutdown_exited).set()

    @server.widget
    def managed(label: str) -> AbstractAsyncContextManager[TrackedWidget]:
        return resource(label)

    async with connected(server) as client:
        idle = await client.call_tool("managed", {"label": "idle"})
        assert idle.isError is False
        with anyio.fail_after(1):
            await idle_exited.wait()
        shutdown = await client.call_tool("managed", {"label": "shutdown"})
        assert shutdown.isError is False

    assert shutdown_exited.is_set()
    assert events == [
        "idle enter",
        "idle close",
        "idle exit",
        "shutdown enter",
        "shutdown close",
        "shutdown exit",
    ]


@pytest.mark.anyio
async def test_factory_owner_finishes_cancellation_safe_acquisition_cleanup() -> None:
    events: list[str] = []
    acquisition_started = anyio.Event()
    allow_yield = anyio.Event()
    owner = _FactoryOwner()

    @asynccontextmanager
    async def resource() -> AsyncGenerator[CounterWidget, None]:
        events.append("enter start")
        acquisition_started.set()
        try:
            await allow_yield.wait()
            yield CounterWidget()
        finally:
            with anyio.CancelScope(shield=True):
                events.append("cleanup start")
                await anyio.sleep(0)
                events.append("cleanup end")

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(owner.run, resource, {})
        await acquisition_started.wait()
        owner.request_close("test cleanup")
        with anyio.fail_after(1):
            await owner.wait_closed()

    assert events == ["enter start", "cleanup start", "cleanup end"]


@pytest.mark.anyio
async def test_factory_owner_cancels_an_awaitable_factory_acquisition() -> None:
    events: list[str] = []
    acquisition_started = anyio.Event()
    owner = _FactoryOwner()

    async def factory() -> CounterWidget:
        events.append("factory start")
        acquisition_started.set()
        try:
            await anyio.sleep_forever()
            raise AssertionError("sleep_forever returned")
        finally:
            with anyio.CancelScope(shield=True):
                events.append("cleanup start")
                await anyio.sleep(0)
                events.append("cleanup end")

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(owner.run, factory, {})
        await acquisition_started.wait()
        owner.request_close("test cleanup")
        with anyio.fail_after(1):
            await owner.wait_closed()

    assert events == ["factory start", "cleanup start", "cleanup end"]


@pytest.mark.anyio
async def test_dispose_and_shutdown_wait_for_the_same_active_session_call() -> None:
    events: list[str] = []
    active = anyio.Event()
    release = anyio.Event()
    dispose_started = anyio.Event()
    dispose_done = anyio.Event()
    close_done = anyio.Event()

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            events.append("widget close")
            super().close()

    @asynccontextmanager
    async def managed() -> AsyncGenerator[TrackedWidget, None]:
        events.append("resource enter")
        try:
            yield TrackedWidget()
        finally:
            events.append("resource exit")

    async with anyio.create_task_group() as task_group:
        runtime = _SessionRuntime(
            task_group,
            session_idle_timeout=30,
            app_uri="ui://test/widget.html",
        )
        launch = await runtime.open(
            managed,
            {},
            DEFAULT_STATE,
            tool_name="managed",
            tool_title="Managed",
        )
        assert launch.meta is not None
        instance_id = launch.meta["anywidget"]["instanceId"]

        async def hold_active_call() -> None:
            with runtime.use(instance_id):
                active.set()
                await release.wait()

        async def dispose() -> None:
            dispose_started.set()
            assert await runtime.dispose(instance_id, "app disposal")
            dispose_done.set()

        async def close() -> None:
            await runtime.aclose()
            close_done.set()

        task_group.start_soon(hold_active_call)
        await active.wait()
        task_group.start_soon(dispose)
        await dispose_started.wait()
        await checkpoint()
        task_group.start_soon(close)
        await checkpoint()

        assert events == ["resource enter"]
        assert not dispose_done.is_set()
        assert not close_done.is_set()

        release.set()
        with anyio.fail_after(1):
            await dispose_done.wait()
            await close_done.wait()

    assert events == ["resource enter", "widget close", "resource exit"]


@pytest.mark.anyio
async def test_aclose_finishes_live_session_cleanup_under_cancellation() -> None:
    events: list[str] = []

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            events.append("widget close")
            super().close()

    @asynccontextmanager
    async def managed() -> AsyncGenerator[TrackedWidget, None]:
        try:
            yield TrackedWidget()
        finally:
            await anyio.sleep(0)
            events.append("resource exit")

    async with anyio.create_task_group() as task_group:
        runtime = _SessionRuntime(
            task_group,
            session_idle_timeout=30,
            app_uri="ui://test/widget.html",
        )
        await runtime.open(
            managed,
            {},
            DEFAULT_STATE,
            tool_name="managed",
            tool_title="Managed",
        )

        with anyio.CancelScope() as cancel_scope:
            cancel_scope.cancel()
            await runtime.aclose()

    assert events == ["widget close", "resource exit"]


@pytest.mark.anyio
async def test_dispose_retries_transient_widget_cleanup_before_forgetting_owner() -> (
    None
):
    class TransientCloseWidget(CounterWidget):
        attempts = 0

        def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("widget close failed")
            super().close()

    widget = TransientCloseWidget()
    async with anyio.create_task_group() as task_group:
        runtime = _SessionRuntime(
            task_group,
            session_idle_timeout=30,
            app_uri="ui://test/widget.html",
        )
        launch = await runtime.open(
            lambda: widget,
            {},
            DEFAULT_STATE,
            tool_name="transient",
            tool_title="Transient",
        )
        assert launch.meta is not None

        assert await runtime.dispose(
            launch.meta["anywidget"]["instanceId"],
            "app disposal",
        )

        assert widget.attempts == 2
        assert widget.comm is None
        task_group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_launch_failure_retries_fresh_root_cleanup() -> None:
    class BrokenLaunchWidget(CounterWidget):
        close_attempts = 0

        def __init__(self) -> None:
            self.fail_send = False
            super().__init__()
            self.fail_send = True

        def send_state(self, *args: Any, **kwargs: Any) -> None:
            super().send_state(*args, **kwargs)
            if self.fail_send:
                raise RuntimeError("initial state failed")

        def close(self) -> None:
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeError("widget close failed")
            super().close()

    widget = BrokenLaunchWidget()
    async with anyio.create_task_group() as task_group:
        runtime = _SessionRuntime(
            task_group,
            session_idle_timeout=30,
            app_uri="ui://test/widget.html",
        )

        with pytest.raises(ExceptionGroup) as error_info:
            await runtime.open(
                lambda: widget,
                {},
                DEFAULT_STATE,
                tool_name="broken",
                tool_title="Broken",
            )
        assert "initial state failed" in leaf_error_messages(error_info.value)

        assert widget.close_attempts == 2
        assert widget.comm is None
        task_group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_launch_validation_closes_managed_widget_once() -> None:
    server = AnyWidgetMCP("test")
    events: list[str] = []

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            events.append("widget close")
            super().close()

    @server.widget(
        state=StateProjection(lambda widget: {"value": widget.value}, watch="missing")
    )
    @contextmanager
    def managed() -> Generator[TrackedWidget]:
        try:
            yield TrackedWidget()
        finally:
            events.append("resource exit")

    async with connected(server) as client:
        result = await client.call_tool("managed", {})

    assert result.isError is True
    assert isinstance(result.content[0], TextContent)
    assert "Unknown state trait" in result.content[0].text
    assert events == ["widget close", "resource exit"]
