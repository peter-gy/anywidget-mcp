from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import patch

import anywidget
from exceptiongroup import ExceptionGroup
from ipywidgets.widgets import widget as widget_module
import pytest
import traitlets as t

from anywidget_mcp import webmcp


class Counter(anywidget.AnyWidget):
    _esm = "export default { render() {} };"
    value = t.Int(0).tag(sync=True)
    doubled = t.Int(0, read_only=True).tag(sync=True)
    notes = t.Unicode("internal").tag(sync=True)
    secret = t.Unicode("secret").tag(sync=True, webmcp=False)

    @t.observe("value")
    def _double(self, change: dict[str, Any]) -> None:
        self.set_trait("doubled", change["new"] * 2)


@pytest.fixture(autouse=True)
def close_test_widgets() -> Iterator[None]:
    before = set(widget_module._instances)
    yield
    webmcp.disable()
    created = [
        widget
        for key, widget in list(widget_module._instances.items())
        if key not in before
    ]
    for widget in created:
        widget.close()


def descriptor(widget: anywidget.AnyWidget) -> Any:
    return widget.get_state().get("_webmcp")


def request(widget: anywidget.AnyWidget, **message: Any) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    assert widget.comm is not None
    with patch.object(
        widget.comm, "send", side_effect=lambda data, **_: messages.append(data)
    ):
        widget._handle_custom_msg(
            {"kind": "anywidget-webmcp", "id": "request", **message}, []
        )
    return messages[-1]["content"]


def create(
    session: webmcp.Session,
    arguments: Any,
    *,
    request_id: str = "request",
    target: str | None = None,
) -> dict[str, Any]:
    return request(
        session,
        id=request_id,
        operation="create",
        target=target or session.get_state()["_webmcp_catalog"][0]["id"],
        arguments=arguments,
    )


def test_curated_session_exposes_explicit_instances_and_skips_discovery() -> None:
    existing = Counter()
    selected = Counter()
    session = webmcp.enable(widgets=[selected, Counter], discover=False)
    later = Counter()
    assert descriptor(existing) is None
    assert descriptor(later) is None
    assert descriptor(selected)["properties"]["value"] == {"type": "integer"}
    assert session.get_state()["_webmcp_widgets"] == [f"anywidget:{selected.model_id}"]
    assert len(session.get_state()["_webmcp_catalog"]) == 1


def test_discovery_applies_class_settings_and_explicit_exclusions() -> None:
    excluded = Counter()
    session = webmcp.enable(widgets={Counter: {"private": {"notes"}}, excluded: False})
    included = Counter()
    assert descriptor(excluded) is None
    assert set(descriptor(included)["properties"]) == {"value", "doubled"}
    assert len(session.get_state()["_webmcp_widgets"]) == 1
    assert webmcp.enable(widgets={Counter: False}) is session
    assert descriptor(included) is None
    assert session.get_state()["_webmcp_catalog"] == []
    assert descriptor(Counter()) is None


def test_instance_privacy_and_read_access_preserve_native_trait_behavior() -> None:
    private = Counter()
    ordinary = Counter()
    webmcp.enable(widgets={private: {"private": {"notes"}, "read_only": {"value"}}})
    assert request(private, operation="read")["result"]["state"] == {
        "value": 0,
        "doubled": 0,
    }
    assert "notes" in request(ordinary, operation="read")["result"]["state"]
    for state in (
        {"notes": "exposed"},
        {"value": 4},
        {"doubled": 8},
        {"secret": "exposed"},
    ):
        assert "error" in request(private, operation="update", state=state)
    private.set_state({"value": 7, "notes": "native update"})
    assert request(private, operation="read")["result"]["state"] == {
        "value": 7,
        "doubled": 14,
    }
    assert private.notes == "native update"
    assert (
        request(ordinary, operation="update", state={"value": 3})["result"]["state"][
            "doubled"
        ]
        == 6
    )


def test_instance_configuration_patches_preserve_identity_and_omitted_options() -> None:
    widget = Counter()
    session = webmcp.enable(
        widgets={widget: {"private": {"notes"}, "name": "review"}}, discover=False
    )
    identity = descriptor(widget)["id"]
    assert (
        webmcp.enable(widgets={widget: {"read_only": True, "title": "Review counter"}})
        is session
    )
    assert descriptor(widget)["id"] == identity
    assert descriptor(widget)["name"] == "review"
    assert descriptor(widget)["title"] == "Review counter"
    assert set(descriptor(widget)["properties"]) == {"value", "doubled"}
    assert descriptor(widget)["writable"] == {}
    assert descriptor(Counter()) is None
    assert webmcp.enable(widgets={widget: False}) is session
    assert descriptor(widget) is None
    webmcp.enable(widgets={widget: {"traits": {"value", "secret"}}})
    assert set(descriptor(widget)["properties"]) == {"value"}


@pytest.mark.parametrize(
    "settings", [{"unknown": True}, {"private": {"missing"}}, {"name": "invalid name"}]
)
def test_rejected_configuration_preserves_active_instance_policy(settings: Any) -> None:
    widget = Counter()
    session = webmcp.enable(widgets={widget: {"private": {"notes"}}}, discover=False)
    previous = descriptor(widget)
    with pytest.raises((TypeError, ValueError)):
        webmcp.enable(widgets={widget: settings}, discover=True)
    assert webmcp.enable() is session
    assert descriptor(widget) == previous
    assert descriptor(Counter()) is None
    assert request(widget, operation="update", state={"value": 4})["result"][
        "state"
    ] == {"value": 4, "doubled": 8}


def test_creation_validates_before_calling_and_returns_configured_live_widget() -> None:
    calls: list[int] = []

    def create_counter(start: int = 2) -> Counter:
        calls.append(start)
        return Counter(value=start)

    session = webmcp.enable(
        widgets={create_counter: {"private": {"notes"}, "read_only": {"value"}}},
        discover=False,
    )
    assert calls == []
    assert session.get_state()["_webmcp_catalog"][0]["inputSchema"]["properties"][
        "start"
    ] == {"type": "integer", "default": 2}
    assert "error" in create(session, {"start": True}, request_id="invalid")
    assert calls == []
    result = create(session, {})["result"]
    assert calls == [2]
    assert result["state"] == {"value": 2, "doubled": 4}
    assert set(result["tools"]) == {"read"}
    assert session.get_state()["_webmcp_created"] == [result["ref"]]
    created = widget_module._instances[result["ref"].removeprefix("anywidget:")]
    assert isinstance(created, Counter)
    assert request(created, operation="read")["result"]["state"] == result["state"]


def test_creation_replays_request_identity_and_rejects_changed_arguments() -> None:
    calls: list[int] = []

    def create_counter(start: int) -> Counter:
        calls.append(start)
        return Counter(value=start)

    session = webmcp.enable(widgets=[create_counter], discover=False)
    first = create(session, {"start": 4})
    assert create(session, {"start": 4}) == first
    assert "error" in create(session, {"start": 5})
    assert calls == [4]
    second = create(session, {"start": 5}, request_id="another")
    assert second["result"]["ref"] != first["result"]["ref"]
    assert calls == [4, 5]


def test_creation_errors_replay_and_existing_results_keep_their_owner() -> None:
    existing = Counter()
    calls = 0

    def reuse() -> Counter:
        nonlocal calls
        calls += 1
        return existing

    session = webmcp.enable(widgets=[reuse], discover=False)
    first = create(session, {})
    assert "fresh widget" in first["error"]
    assert create(session, {}) == first
    assert calls == 1
    session.close()
    assert existing.comm is not None
    assert not webmcp.is_enabled()


def test_creation_rejects_nonwidget_results_and_cleans_failed_policy_results() -> None:
    returned: list[Counter] = []

    def invalid() -> str:
        return "counter"

    def unknown_traits() -> Counter:
        widget = Counter()
        returned.append(widget)
        return widget

    session = webmcp.enable(
        widgets={invalid: {}, unknown_traits: {"private": {"missing"}}}, discover=False
    )
    catalog = session.get_state()["_webmcp_catalog"]
    assert (
        "return an AnyWidget" in create(session, {}, target=catalog[0]["id"])["error"]
    )
    assert (
        "Unknown WebMCP traits"
        in create(session, {}, target=catalog[1]["id"], request_id="policy")["error"]
    )
    assert returned[0].comm is None
    assert session.get_state()["_webmcp_created"] == []


def test_async_creation_shares_pending_requests_and_cancels_on_disable() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        delivered = asyncio.Event()
        calls = 0
        messages: list[dict[str, Any]] = []

        async def create_counter(start: int = 3) -> Counter:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return Counter(value=start)

        session = webmcp.enable(widgets=[create_counter], discover=False)
        target = session.get_state()["_webmcp_catalog"][0]["id"]
        message = {
            "kind": "anywidget-webmcp",
            "id": "async",
            "operation": "create",
            "target": target,
            "arguments": {},
        }

        def capture(data: dict[str, Any], **_: Any) -> None:
            if data.get("method") == "custom":
                messages.append(data["content"])
                delivered.set()

        assert session.comm is not None
        with patch.object(session.comm, "send", side_effect=capture):
            session._handle_custom_msg(message, [])
            await asyncio.wait_for(entered.wait(), 2)
            session._handle_custom_msg(message, [])
            assert calls == 1
            assert messages == []
            session._handle_custom_msg({**message, "arguments": {"start": 8}}, [])
            assert "different arguments" in messages[-1]["error"]
            delivered.clear()
            release.set()
            await asyncio.wait_for(delivered.wait(), 2)
            assert messages[-1]["result"]["state"]["value"] == 3
            delivered.clear()
            entered.clear()
            release.clear()
            session._handle_custom_msg({**message, "id": "cancel"}, [])
            await asyncio.wait_for(entered.wait(), 2)
            webmcp.disable()
            await asyncio.wait_for(delivered.wait(), 2)
            assert messages[-1] == {
                "kind": "anywidget-webmcp-result",
                "id": "cancel",
                "error": "Widget creation was cancelled",
            }
        assert len(session.get_state()["_webmcp_created"]) == 1
        session.close()

    asyncio.run(scenario())


def test_disable_preserves_created_widgets_until_session_close() -> None:
    existing = Counter()
    session = webmcp.enable(widgets=[Counter, existing], discover=False)
    result = create(session, {})["result"]
    created = widget_module._instances[result["ref"].removeprefix("anywidget:")]
    assert isinstance(created, Counter)
    webmcp.disable()
    assert not webmcp.is_enabled()
    assert created.comm is not None
    assert existing.comm is not None
    assert descriptor(created) is None
    assert "disabled" in create(session, {}, request_id="disabled")["error"]
    session.close()
    assert created.comm is None
    assert existing.comm is not None


def test_creation_registration_updates_keep_identity_and_refresh_created_widgets() -> (
    None
):
    def create_counter() -> Counter:
        return Counter()

    session = webmcp.enable(widgets=[create_counter], discover=False)
    catalog = session.get_state()["_webmcp_catalog"][0]
    result = create(session, {})["result"]
    created = widget_module._instances[result["ref"].removeprefix("anywidget:")]
    assert isinstance(created, Counter)
    identity = descriptor(created)["id"]
    assert "notes" in result["state"]
    webmcp.enable(
        widgets={create_counter: {"private": {"notes"}, "title": "Counter for review"}}
    )
    assert session.get_state()["_webmcp_catalog"][0]["id"] == catalog["id"]
    assert descriptor(created)["id"] == identity
    assert descriptor(created)["title"] == "Counter for review"
    assert request(created, operation="read")["result"]["state"] == {
        "value": 0,
        "doubled": 0,
    }


def test_duplicate_creation_names_reject_the_complete_configuration_update() -> None:
    def first() -> Counter:
        return Counter()

    def second() -> Counter:
        return Counter()

    session = webmcp.enable(widgets={first: {"name": "counter"}}, discover=False)
    previous = session.get_state()["_webmcp_catalog"]
    with pytest.raises(ValueError, match="Duplicate WebMCP creation tool name"):
        webmcp.enable(
            widgets={first: {"private": {"notes"}}, second: {"name": "counter"}}
        )
    assert session.get_state()["_webmcp_catalog"] == previous
    assert "notes" in create(session, {})["result"]["state"]


def test_session_closes_created_graph_and_preserves_preexisting_children() -> None:
    from ipywidgets import Widget
    from ipywidgets.widgets.widget import widget_serialization

    class Composite(Counter):
        children = t.List(t.Instance(Widget)).tag(sync=True, **widget_serialization)

    existing = Counter()
    children: list[Counter] = []

    def create_composite() -> Composite:
        child = Counter()
        children.append(child)
        return Composite(children=[existing, child])

    session = webmcp.enable(widgets=[create_composite], discover=False)
    result = create(session, {})["result"]
    root = widget_module._instances[result["ref"].removeprefix("anywidget:")]
    session.close()
    assert root.comm is None
    assert children[0].comm is None
    assert existing.comm is not None


def test_discovery_waits_for_factory_policy_before_publishing_created_widget() -> None:
    async def scenario() -> None:
        constructed = asyncio.Event()
        release = asyncio.Event()
        delivered = asyncio.Event()
        created: list[Counter] = []
        messages: list[dict[str, Any]] = []

        async def create_counter() -> Counter:
            result = Counter(notes="factory-private")
            created.append(result)
            constructed.set()
            await release.wait()
            return result

        session = webmcp.enable(widgets={create_counter: {"private": {"notes"}}})
        target = session.get_state()["_webmcp_catalog"][0]["id"]

        def capture(data: dict[str, Any], **_: Any) -> None:
            if data.get("method") == "custom":
                messages.append(data["content"])
                delivered.set()

        assert session.comm is not None
        with patch.object(session.comm, "send", side_effect=capture):
            session._handle_custom_msg(
                {
                    "kind": "anywidget-webmcp",
                    "id": "private",
                    "operation": "create",
                    "target": target,
                    "arguments": {},
                },
                [],
            )
            await asyncio.wait_for(constructed.wait(), 2)
            assert descriptor(created[0]) is None
            assert (
                f"anywidget:{created[0].model_id}"
                not in session.get_state()["_webmcp_widgets"]
            )
            release.set()
            await asyncio.wait_for(delivered.wait(), 2)
            assert messages[-1]["result"]["state"] == {"value": 0, "doubled": 0}
            assert set(descriptor(created[0])["properties"]) == {"value", "doubled"}
        session.close()

    asyncio.run(scenario())


def test_session_preserves_borrowed_protocol_child() -> None:
    from anywidget._descriptor import MimeBundleDescriptor

    class Child(t.HasTraits):
        value = t.Int(0).tag(sync=True)
        _repr_mimebundle_ = MimeBundleDescriptor(esm="export default { render() {} };")

    class Parent(Counter):
        child = anywidget.WidgetTrait().tag(sync=True)

    child = Child()
    child._repr_mimebundle_()

    def create_parent() -> Parent:
        return Parent(child=child)

    session = webmcp.enable(widgets=[create_parent], discover=False)
    assert "result" in create(session, {})
    controller = child._repr_mimebundle_
    with patch.object(controller._comm, "close") as close:
        session.close()
        close.assert_not_called()
    controller.unsync_object_with_view()
    controller._comm.close()


def test_async_creation_preserves_widget_constructed_by_another_task() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        delivered = asyncio.Event()
        external: list[Counter] = []
        responses: list[dict[str, Any]] = []

        async def reuse_counter() -> Counter:
            entered.set()
            await release.wait()
            return external[0]

        session = webmcp.enable(widgets=[reuse_counter], discover=False)
        target = session.get_state()["_webmcp_catalog"][0]["id"]

        def capture(data: dict[str, Any], **_: Any) -> None:
            if data.get("method") == "custom":
                responses.append(data["content"])
                delivered.set()

        assert session.comm is not None
        with patch.object(session.comm, "send", side_effect=capture):
            session._handle_custom_msg(
                {
                    "kind": "anywidget-webmcp",
                    "id": "reuse",
                    "operation": "create",
                    "target": target,
                    "arguments": {},
                },
                [],
            )
            await asyncio.wait_for(entered.wait(), 2)
            external.append(Counter())
            release.set()
            await asyncio.wait_for(delivered.wait(), 2)
        assert "fresh widget" in responses[-1]["error"]
        session.close()
        assert external[0].comm is not None
        external[0].value = 9
        assert external[0].doubled == 18

    asyncio.run(scenario())


@pytest.mark.parametrize("replace_session", [False, True])
def test_background_widget_after_factory_completion_uses_current_discovery(
    replace_session: bool,
) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        delivered = asyncio.Event()
        tasks: list[asyncio.Task[Counter]] = []

        async def later() -> Counter:
            await release.wait()
            return Counter()

        def create_counter() -> Counter:
            tasks.append(asyncio.create_task(later()))
            return Counter()

        session = webmcp.enable(widgets=[create_counter])
        active = session

        def capture(data: dict[str, Any], **_: Any) -> None:
            if data.get("method") == "custom":
                delivered.set()

        assert session.comm is not None
        with patch.object(session.comm, "send", side_effect=capture):
            session._handle_custom_msg(
                {
                    "kind": "anywidget-webmcp",
                    "id": "background",
                    "operation": "create",
                    "target": session.get_state("_webmcp_catalog")["_webmcp_catalog"][
                        0
                    ]["id"],
                    "arguments": {},
                },
                [],
            )
            await asyncio.wait_for(delivered.wait(), 2)
        if replace_session:
            webmcp.disable()
            active = webmcp.enable()
        release.set()
        widget = await asyncio.wait_for(tasks[0], 2)
        assert descriptor(widget) is not None
        assert (
            f"anywidget:{widget.model_id}"
            in active.get_state("_webmcp_widgets")["_webmcp_widgets"]
        )
        assert request(widget, operation="read")["result"]["state"] == {
            "value": 0,
            "doubled": 0,
            "notes": "internal",
        }
        active.close()
        session.close()
        assert widget.comm is not None

    asyncio.run(scenario())


def test_failed_creation_closes_every_widget_and_retains_failed_cleanup() -> None:
    class RetryClose(Counter):
        attempts = 0

        def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("host unavailable")
            super().close()

    widgets: list[Counter] = []

    def fail_creation() -> Counter:
        widgets.extend([Counter(), RetryClose()])
        raise ValueError("Creation failed")

    session = webmcp.enable(widgets=[fail_creation], discover=False)
    reply = create(session, {})
    assert "Creation failed" in reply["error"]
    assert "Could not close 1 widget" in reply["error"]
    assert widgets[0].comm is None
    assert widgets[1].comm is not None
    assert create(session, {}) == reply
    session.close()
    assert widgets[1].comm is None


def test_session_close_retries_failed_widgets_after_closing_other_resources() -> None:
    class RetryClose(Counter):
        attempts = 0

        def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("transport unavailable")
            super().close()

    session = webmcp.enable(widgets=[RetryClose, Counter], discover=False)
    catalog = session.get_state()["_webmcp_catalog"]
    failed = create(session, {}, target=catalog[0]["id"])["result"]
    healthy = create(session, {}, target=catalog[1]["id"], request_id="healthy")[
        "result"
    ]
    failed_widget = widget_module._instances[failed["ref"].removeprefix("anywidget:")]
    healthy_widget = widget_module._instances[healthy["ref"].removeprefix("anywidget:")]
    with pytest.raises(ExceptionGroup, match="Failed to close WebMCP session"):
        session.close()
    assert healthy_widget.comm is None
    assert session.comm is None
    assert failed_widget.comm is not None
    assert not webmcp.is_enabled()
    session.close()
    assert failed_widget.comm is None


def test_session_close_releases_comm_when_publishing_cleanup_fails() -> None:
    session = webmcp.enable(widgets=[Counter], discover=False)
    result = create(session, {})["result"]
    widget = widget_module._instances[result["ref"].removeprefix("anywidget:")]
    assert session.comm is not None
    with patch.object(
        session.comm, "send", side_effect=RuntimeError("transport unavailable")
    ):
        with pytest.raises(ExceptionGroup, match="Failed to close WebMCP session"):
            session.close()
    assert widget.comm is None
    assert session.comm is None
    assert not webmcp.is_enabled()


@pytest.mark.parametrize("retry_close", [False, True])
def test_closed_session_cleans_factory_that_suppresses_cancellation(
    retry_close: bool,
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        completed = asyncio.Event()
        widgets: list[Counter] = []

        class LateCounter(Counter):
            attempts = 0

            def close(self) -> None:
                self.attempts += 1
                if retry_close and self.attempts == 1:
                    completed.set()
                    raise RuntimeError("transport unavailable")
                super().close()
                completed.set()

        async def create_counter() -> Counter:
            widgets.append(Counter())
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            result = LateCounter()
            widgets.append(result)
            return result

        session = webmcp.enable(widgets=[create_counter], discover=False)
        session._handle_custom_msg(
            {
                "kind": "anywidget-webmcp",
                "id": "late",
                "operation": "create",
                "target": session.get_state()["_webmcp_catalog"][0]["id"],
                "arguments": {},
            },
            [],
        )
        await asyncio.wait_for(entered.wait(), 2)
        session.close()
        await asyncio.wait_for(cancelled.wait(), 2)
        replacement = webmcp.enable()
        release.set()
        await asyncio.wait_for(completed.wait(), 2)
        if retry_close:
            assert widgets[-1].comm is not None
            session.close()
        assert all(widget.comm is None for widget in widgets)
        assert replacement.get_state()["_webmcp_widgets"] == []
        replacement.close()

    asyncio.run(scenario())


def test_disable_with_failed_delivery_withdraws_access_and_discovery() -> None:
    borrowed = Counter()
    session = webmcp.enable()
    assert descriptor(borrowed) is not None
    assert session.comm is not None
    with patch.object(
        session.comm, "send", side_effect=RuntimeError("transport unavailable")
    ):
        with pytest.raises(ExceptionGroup, match="Failed to disable WebMCP session"):
            webmcp.disable()
    assert not webmcp.is_enabled()
    assert descriptor(borrowed) is None
    assert descriptor(Counter()) is None
    replacement = webmcp.enable(widgets=[borrowed], discover=False)
    assert (
        request(borrowed, operation="update", state={"value": 5})["result"]["state"][
            "value"
        ]
        == 5
    )
    session.close()
    assert webmcp.is_enabled()
    assert descriptor(borrowed) is not None
    replacement.close()
    assert borrowed.comm is not None


def test_pending_creation_limit_recovers_after_completion() -> None:
    async def scenario() -> None:
        started = asyncio.Queue[None]()
        replies = asyncio.Queue[dict[str, Any]]()
        release = asyncio.Event()
        calls = 0

        async def create_counter() -> Counter:
            nonlocal calls
            calls += 1
            started.put_nowait(None)
            await release.wait()
            return Counter()

        session = webmcp.enable(widgets=[create_counter], discover=False)
        message = {
            "kind": "anywidget-webmcp",
            "operation": "create",
            "target": session.get_state()["_webmcp_catalog"][0]["id"],
            "arguments": {},
        }

        def capture(data: dict[str, Any], **_: Any) -> None:
            if data.get("method") == "custom":
                replies.put_nowait(data["content"])

        assert session.comm is not None
        with patch.object(session.comm, "send", side_effect=capture):
            for index in range(32):
                session._handle_custom_msg({**message, "id": str(index)}, [])
            for _ in range(32):
                await asyncio.wait_for(started.get(), 2)
            session._handle_custom_msg({**message, "id": "overflow"}, [])
            assert "Too many" in (await asyncio.wait_for(replies.get(), 2))["error"]
            assert calls == 32
            release.set()
            for _ in range(32):
                assert "result" in await asyncio.wait_for(replies.get(), 2)
            session._handle_custom_msg({**message, "id": "recovered"}, [])
            assert "result" in await asyncio.wait_for(replies.get(), 2)
            assert calls == 33
        session.close()

    asyncio.run(scenario())


def test_invalid_creation_envelopes_leave_registration_ready_for_valid_calls() -> None:
    calls: list[int] = []

    def create_counter(start: int = 1) -> Counter:
        calls.append(start)
        return Counter(value=start)

    session = webmcp.enable(widgets=[create_counter], discover=False)
    nested: Any = 0
    for _ in range(1100):
        nested = cast(Any, [nested])
    for index, arguments in enumerate(
        [{"start": "x" * 65536}, {"start": float("nan")}, {"start": nested}]
    ):
        assert "error" in create(session, arguments, request_id=f"invalid-{index}")
    assert calls == []
    assert (
        create(session, {"start": 3}, request_id="valid")["result"]["state"]["value"]
        == 3
    )
    assert calls == [3]


def test_reactivated_session_rejects_creation_from_previous_activation() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        replies = asyncio.Queue[dict[str, Any]]()
        widgets: list[Counter] = []

        async def create_counter() -> Counter:
            result = Counter()
            widgets.append(result)
            if len(widgets) == 1:
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
            return result

        session = webmcp.enable(widgets=[create_counter], discover=False)
        message = {
            "kind": "anywidget-webmcp",
            "operation": "create",
            "target": session.get_state()["_webmcp_catalog"][0]["id"],
            "arguments": {},
        }

        def capture(data: dict[str, Any], **_: Any) -> None:
            if data.get("method") == "custom":
                replies.put_nowait(data["content"])

        assert session.comm is not None
        with patch.object(session.comm, "send", side_effect=capture):
            session._handle_custom_msg({**message, "id": "previous"}, [])
            await asyncio.wait_for(entered.wait(), 2)
            session.disable()
            await asyncio.wait_for(cancelled.wait(), 2)
            assert webmcp.enable() is session
            release.set()
            previous = await asyncio.wait_for(replies.get(), 2)
            assert "withdrawn" in previous["error"]
            assert widgets[0].comm is None
            assert session.get_state()["_webmcp_created"] == []
            session._handle_custom_msg({**message, "id": "current"}, [])
            current = await asyncio.wait_for(replies.get(), 2)
            assert current["result"]["state"]["value"] == 0
            assert session.get_state()["_webmcp_created"] == [current["result"]["ref"]]
        session.close()

    asyncio.run(scenario())
