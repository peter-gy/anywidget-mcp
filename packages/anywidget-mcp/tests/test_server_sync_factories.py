from __future__ import annotations

import asyncio
import threading
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

import anyio
import pytest

from anywidget_mcp import AnyWidgetMCP
from anywidget_mcp._factory import FactoryOwner

from ._server_support import (
    CounterWidget,
    ParentWidget,
    bootstrap_runtime,
    connected,
    state_id,
)


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["factory", "manager-enter", "manager-exit"])
async def test_sync_factory_lifecycle_keeps_existing_session_state_readable(
    phase: str,
) -> None:
    server = AnyWidgetMCP("concurrent widgets")
    events: list[str] = []
    started = threading.Event()
    release = threading.Event()

    @server.widget
    def existing() -> CounterWidget:
        return CounterWidget(value=7)

    def block_work() -> None:
        events.append("work started")
        started.set()
        # A blocked event loop must finish so the event-order assertion can fail.
        release.wait(5)
        events.append("work finished")

    def construct() -> CounterWidget:
        block_work()
        return CounterWidget()

    @contextmanager
    def resource() -> Generator[CounterWidget]:
        if phase == "manager-exit":
            try:
                yield CounterWidget()
            finally:
                block_work()
        else:
            yield construct()

    server.widget(construct if phase == "factory" else resource, name="slow")
    async with connected(server) as client:
        launch = await client.call_tool("existing", {})

        async def open_slow() -> None:
            opened = await client.call_tool("slow", {})
            if phase == "manager-exit":
                runtime = await bootstrap_runtime(client, opened)
                await client.call_tool(
                    "anywidget_dispose", {"session_id": runtime["instanceId"]}
                )

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(open_slow)
            assert await anyio.to_thread.run_sync(started.wait, 5)
            try:
                result = await client.call_tool(
                    "anywidget_state", {"state_id": state_id(launch)}
                )
                assert result.structured_content is not None
                assert result.structured_content["state"]["value"] == 7
                events.append("existing state read")
            finally:
                release.set()

    assert events == [
        "work started",
        "existing state read",
        "work finished",
    ]


@pytest.mark.anyio
async def test_disposal_releases_resource_when_acquisition_occupies_worker_pool() -> (
    None
):
    server = AnyWidgetMCP("pooled resources")
    limiter = anyio.to_thread.current_default_thread_limiter()
    original_tokens = limiter.total_tokens
    semaphore = threading.Semaphore(1)
    waiting = anyio.Event()
    events: list[str] = []

    @server.widget
    @contextmanager
    def managed(number: int) -> Generator[CounterWidget]:
        if number == 2:
            anyio.from_thread.run_sync(waiting.set)
        semaphore.acquire()
        events.append(f"entered {number}")
        try:
            yield CounterWidget(value=number)
        finally:
            events.append(f"exited {number}")
            semaphore.release()

    def safety_release() -> None:
        events.append("safety release")
        semaphore.release()

    timer = threading.Timer(5, safety_release)
    timer.daemon = True
    limiter.total_tokens = 1
    try:
        async with connected(server) as client:
            first = await client.call_tool("managed", {"number": 1})
            runtime = await bootstrap_runtime(client, first)

            async def open_second() -> None:
                result = await client.call_tool("managed", {"number": 2})
                assert result.structured_content is not None
                assert result.structured_content["state"]["value"] == 2

            timer.start()
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(open_second)
                await waiting.wait()
                disposed = await client.call_tool(
                    "anywidget_dispose", {"session_id": runtime["instanceId"]}
                )
                assert disposed.structured_content == {"disposed": True}
            timer.cancel()
    finally:
        timer.cancel()
        limiter.total_tokens = original_tokens

    assert events == ["entered 1", "exited 1", "entered 2", "exited 2"]


@pytest.mark.anyio
@pytest.mark.parametrize("managed", [False, True], ids=["factory", "manager-enter"])
@pytest.mark.parametrize("native", [False, True], ids=["owner-close", "task-cancel"])
async def test_cancelled_sync_acquisition_closes_eventual_graph(
    managed: bool, native: bool
) -> None:
    events: list[str] = []
    started = threading.Event()
    release = threading.Event()
    owner = FactoryOwner()

    class Child(CounterWidget):
        def close(self) -> None:
            if self.comm is not None:
                events.append("child closed")
            super().close()

    class Parent(ParentWidget):
        def close(self) -> None:
            if self.comm is not None:
                events.append("parent closed")
            super().close()

    def construct() -> Parent:
        started.set()
        release.wait(5)
        events.append("graph constructed")
        return Parent(child=Child())

    @contextmanager
    def resource() -> Generator[Parent]:
        try:
            yield construct()
        finally:
            events.append("manager exited")

    task = asyncio.create_task(owner.run(resource if managed else construct, {}))
    try:
        assert await anyio.to_thread.run_sync(started.wait, 5)
        if native:
            task.cancel("launch cancelled")
        else:
            owner.request_close("launch cancelled")
    finally:
        release.set()
    await task

    with pytest.raises(RuntimeError, match="closed before yielding"):
        await owner.wait_ready()
    assert events == [
        "graph constructed",
        "child closed",
        "parent closed",
        *(["manager exited"] if managed else []),
    ]


@pytest.mark.anyio
async def test_cancelled_tool_call_cleans_sync_acquisition_before_server_shutdown() -> (
    None
):
    server = AnyWidgetMCP("cancelled widget")
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    events: list[str] = []

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            if self.comm is not None:
                events.append("widget closed")
            super().close()

    @server.widget
    @contextmanager
    def managed() -> Generator[TrackedWidget]:
        started.set()
        release.wait(5)
        try:
            yield TrackedWidget()
        finally:
            events.append("manager exited")
            closed.set()

    async with connected(server) as client:
        task = asyncio.create_task(client.call_tool("managed", {}))
        try:
            assert await anyio.to_thread.run_sync(started.wait, 5)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await anyio.to_thread.run_sync(closed.wait, 5)
        assert events == ["widget closed", "manager exited"]


@pytest.mark.anyio
@pytest.mark.parametrize("async_factory", [False, True], ids=["sync", "async"])
async def test_factory_context_reaches_sync_manager_entry_and_exit(
    async_factory: bool,
) -> None:
    server = AnyWidgetMCP("factory context")
    value: ContextVar[str] = ContextVar("factory_value", default="request")
    events: list[str] = []

    @contextmanager
    def resource() -> Generator[CounterWidget]:
        events.append(value.get())
        token = value.set("manager")
        try:
            yield CounterWidget()
        finally:
            events.append(value.get())
            value.reset(token)
            events.append(value.get())

    def factory():
        value.set("factory")
        return resource()

    async def awaitable_factory():
        value.set("factory")
        return resource()

    server.widget(awaitable_factory if async_factory else factory, name="managed")

    async with connected(server) as client:
        await client.call_tool("managed", {})

    assert events == ["factory", "manager", "factory"]
    assert value.get() == "request"
