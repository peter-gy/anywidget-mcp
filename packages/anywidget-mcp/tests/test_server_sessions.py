from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
import httpx2 as httpx
import pytest
from anyio.lowlevel import checkpoint
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

import anywidget_mcp._runtime as runtime_module
from anywidget_mcp import AnyWidgetMCP, attach
from anywidget_mcp._runtime import SessionRuntime
from anywidget_mcp._state import DEFAULT_STATE

from ._server_support import (
    CounterWidget,
    ParentWidget,
    bootstrap_id,
    bootstrap_runtime,
    connected,
)


@asynccontextmanager
async def http_connected(
    http: httpx.AsyncClient,
) -> AsyncGenerator[ClientSession, None]:
    async with streamable_http_client(
        "http://localhost:8000/mcp",
        http_client=http,
    ) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


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
            results.append(first.is_error is False)
            borrower_connected.set()
            await check_borrower.wait()
            second = await client.call_tool("counter", {"value": 2})
            results.append(second.is_error is False)
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


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("stateless_http", [False, True])
@pytest.mark.anyio
async def test_http_app_keeps_widget_sessions_across_mcp_connections(
    stateless_http: bool,
    attached: bool,
) -> None:
    if attached:
        server = MCPServer("test")
        widgets = attach(server)
    else:
        anywidget_server = AnyWidgetMCP("test")
        server = anywidget_server
        widgets = anywidget_server

    @widgets.widget
    def counter() -> CounterWidget:
        return CounterWidget(value=3)

    app = server.streamable_http_app(stateless_http=stateless_http)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost:8000",
        ) as http:
            async with http_connected(http) as client:
                launch = await client.call_tool("counter", {})

            async with http_connected(http) as client:
                runtime = await bootstrap_runtime(client, launch)
                instance_id = runtime["instanceId"]
                poll = await client.call_tool(
                    "anywidget_poll",
                    {
                        "instance_id": instance_id,
                        "operation_id": 1,
                    },
                )

    assert poll.is_error is False


@pytest.mark.anyio
async def test_bootstrap_claim_replays_for_one_operation() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget)

    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {})
        token = bootstrap_id(launch)
        arguments = {"bootstrap_id": token, "operation_id": "claim"}
        invalid = await client.call_tool(
            "anywidget_bootstrap",
            {"bootstrap_id": token, "operation_id": ""},
        )
        first = await client.call_tool("anywidget_bootstrap", arguments)
        assert first.meta is not None
        first.meta["anywidget"]["models"].clear()

        replay = await client.call_tool("anywidget_bootstrap", arguments)
        conflict = await client.call_tool(
            "anywidget_bootstrap",
            {"bootstrap_id": token, "operation_id": "other-claim"},
        )

    assert invalid.is_error is True
    assert replay.is_error is False
    assert replay.meta is not None
    assert replay.meta["anywidget"]["models"]
    assert conflict.is_error is True
    assert isinstance(conflict.content[0], TextContent)
    assert "claimed by another operation" in conflict.content[0].text


@pytest.mark.anyio
async def test_rejected_claim_cannot_dispose_the_accepted_session() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget)

    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {})
        token = bootstrap_id(launch)
        runtime = await bootstrap_runtime(client, launch, operation_id="claim-one")
        rejected = await client.call_tool(
            "anywidget_bootstrap",
            {"bootstrap_id": token, "operation_id": "claim-two"},
        )
        refused_dispose = await client.call_tool(
            "anywidget_dispose",
            {"session_id": token, "operation_id": "claim-two"},
        )
        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": runtime["instanceId"],
                "operation_id": 1,
            },
        )

    assert rejected.is_error is True
    assert refused_dispose.is_error is True
    assert poll.is_error is False


@pytest.mark.parametrize("token", ["invalid", "A" * 32, "0" * 31, "0" * 32])
@pytest.mark.anyio
async def test_bootstrap_rejects_unavailable_capabilities_generically(
    token: str,
) -> None:
    server = AnyWidgetMCP("test")

    async with connected(server) as client:
        result = await client.call_tool(
            "anywidget_bootstrap",
            {"bootstrap_id": token, "operation_id": "claim"},
        )

    assert result.is_error is True
    assert isinstance(result.content[0], TextContent)
    assert "Widget bootstrap is unavailable" in result.content[0].text
    assert token not in result.content[0].text


@pytest.mark.anyio
async def test_successful_session_call_consumes_bootstrap_claim() -> None:
    server = AnyWidgetMCP("test")
    server.widget(CounterWidget)

    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {})
        token = bootstrap_id(launch)
        runtime = await bootstrap_runtime(client, launch, operation_id="claim")
        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": runtime["instanceId"],
                "operation_id": 1,
            },
        )
        replay = await client.call_tool(
            "anywidget_bootstrap",
            {"bootstrap_id": token, "operation_id": "claim"},
        )

    assert poll.is_error is False
    assert replay.is_error is True
    assert isinstance(replay.content[0], TextContent)
    assert "Widget bootstrap is unavailable" in replay.content[0].text


@pytest.mark.anyio
async def test_configured_idle_lifetime_applies_from_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    clock = [0.0]
    monkeypatch.setattr(
        runtime_module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])
    )
    server = AnyWidgetMCP("idle-policy", session_idle_timeout=60)
    server.widget(CounterWidget)
    async with connected(server) as client:
        launch = await client.call_tool("counter_widget", {})
        active = server._widget_tools._require_runtime()
        lease = active._bootstraps[bootstrap_id(launch)]
        assert lease.deadline == 60
        clock[0] = 31.0
        runtime = await bootstrap_runtime(client, launch)
        assert runtime["sessionIdleTimeoutMs"] == 60_000
        assert lease.deadline == 91


@pytest.mark.anyio
async def test_lifespan_owned_widget_survives_age_and_closes_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    clock = [0.0]
    monkeypatch.setattr(
        runtime_module, "time", types.SimpleNamespace(monotonic=lambda: clock[0])
    )
    widget = CounterWidget()
    server = AnyWidgetMCP("durable", session_idle_timeout=None)
    server.widget(lambda: widget, name="counter")
    async with connected(server) as client:
        launch = await client.call_tool("counter", {})
        active = server._widget_tools._require_runtime()
        assert active._bootstraps[bootstrap_id(launch)].deadline is None
        clock[0] = 1_000_000.0
        runtime = await bootstrap_runtime(client, launch)
        assert "sessionIdleTimeoutMs" not in runtime
        assert not (
            await client.call_tool(
                "anywidget_poll",
                {"instance_id": runtime["instanceId"], "operation_id": 1},
            )
        ).is_error
        assert widget.comm is not None
        await server.aclose()
        assert widget.comm is None


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
    original_aclose = SessionRuntime.aclose
    close_calls = 0

    async def controlled_aclose(runtime: SessionRuntime) -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            close_started.set()
            await release_close.wait()
        await original_aclose(runtime)

    monkeypatch.setattr(SessionRuntime, "aclose", controlled_aclose)

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
        runtime = await bootstrap_runtime(client, launch)
        instance_id = runtime["instanceId"]
        first = await client.call_tool("anywidget_dispose", {"session_id": instance_id})
        second = await client.call_tool(
            "anywidget_dispose", {"session_id": instance_id}
        )
        poll = await client.call_tool(
            "anywidget_poll",
            {
                "instance_id": instance_id,
                "operation_id": 1,
            },
        )

    assert first.structured_content == {"disposed": True}
    assert second.structured_content == {"disposed": False}
    assert poll.is_error is True
    assert isinstance(poll.content[0], TextContent)
    assert "Unknown widget session" in poll.content[0].text


@pytest.mark.anyio
async def test_dispose_accepts_an_unclaimed_bootstrap_capability() -> None:
    closed = anyio.Event()

    class TrackedWidget(CounterWidget):
        def close(self) -> None:
            super().close()
            closed.set()

    server = AnyWidgetMCP("test")
    server.widget(TrackedWidget)

    async with connected(server) as client:
        launch = await client.call_tool("tracked_widget", {})
        token = bootstrap_id(launch)
        missing_operation = await client.call_tool(
            "anywidget_dispose",
            {"session_id": token},
        )
        disposed = await client.call_tool(
            "anywidget_dispose",
            {"session_id": token, "operation_id": "abandon"},
        )
        bootstrap = await client.call_tool(
            "anywidget_bootstrap",
            {"bootstrap_id": token, "operation_id": "after-dispose"},
        )

    assert missing_operation.is_error is True
    assert disposed.structured_content == {"disposed": True}
    assert closed.is_set()
    assert bootstrap.is_error is True
    assert isinstance(bootstrap.content[0], TextContent)
    assert "Widget bootstrap is unavailable" in bootstrap.content[0].text


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
        token = bootstrap_id(launch)
        with anyio.fail_after(1):
            await closed.wait()
        bootstrap = await client.call_tool(
            "anywidget_bootstrap",
            {
                "bootstrap_id": token,
                "operation_id": "expired-session",
            },
        )

    assert bootstrap.is_error is True
    assert isinstance(bootstrap.content[0], TextContent)
    assert "Widget bootstrap is unavailable" in bootstrap.content[0].text


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

        assert first.is_error is False
        assert second.is_error is False
        assert all(widget.comm is not None for widget in created)
        await server.aclose()

    assert all(widget.comm is None for widget in created)


@pytest.mark.anyio
async def test_active_session_call_is_not_expired() -> None:
    active = anyio.Event()
    release = anyio.Event()
    idle_closed = anyio.Event()

    class IdleWidget(CounterWidget):
        def close(self) -> None:
            super().close()
            idle_closed.set()

    async with anyio.create_task_group() as task_group:
        runtime = SessionRuntime(
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
        bootstrap = runtime.bootstrap(bootstrap_id(launch), "active-session")
        assert bootstrap.meta is not None
        instance_id = bootstrap.meta["anywidget"]["instanceId"]

        async def hold_call() -> None:
            with runtime.use(instance_id):
                active.set()
                await release.wait()

        task_group.start_soon(hold_call)
        await active.wait()
        await runtime.open(
            IdleWidget,
            {},
            DEFAULT_STATE,
            tool_name="idle_counter",
            tool_title="Idle Counter",
        )
        with anyio.fail_after(2):
            await idle_closed.wait()

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

        assert first.is_error is False
        assert second.is_error is False
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
        payload = await bootstrap_runtime(client, launch)
        reused = await client.call_tool("counter", {})
        comm = await client.comm(
            {
                "instance_id": payload["instanceId"],
                "model_id": payload["rootModelId"],
                "operation_id": 1,
                "data": {"method": "request_state"},
            },
        )

    assert reused.is_error is True
    assert isinstance(reused.content[0], TextContent)
    assert "Return a fresh root" in reused.content[0].text
    assert comm.is_error is False


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
        payload = await bootstrap_runtime(client, launch)
        reused = await client.call_tool("parent", {})
        comm = await client.comm(
            {
                "instance_id": payload["instanceId"],
                "model_id": child.model_id,
                "operation_id": 1,
                "data": {"method": "request_state"},
            },
        )

        assert roots[1].comm is None
        assert reused.is_error is True
        assert isinstance(reused.content[0], TextContent)
        assert "fresh nested widgets" in reused.content[0].text
        assert comm.is_error is False
