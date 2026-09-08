from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from types import ModuleType
from typing import Any
from unittest.mock import patch

import anywidget
import comm
from ipywidgets import Widget
from ipywidgets.widgets import widget as widget_module
import pytest
import traitlets as t

from anywidget_mcp import webmcp
from anywidget_mcp._webmcp.host import capture_host


class LifecycleRegistry:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def add(self, item: Any) -> None:
        self.items.append(item)

    def dispose(self, context: Context) -> None:
        for item in self.items:
            assert item.dispose(context, deletion=False)


class Context:
    cell_id: str | None = "configure"

    def __init__(self) -> None:
        self.cell_lifecycle_registry = LifecycleRegistry()
        self.stream = object()

    @contextmanager
    def with_cell_id(self, cell_id: str) -> Generator[None, None, None]:
        previous = self.cell_id
        self.cell_id = cell_id
        try:
            yield
        finally:
            self.cell_id = previous


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> Context:
    context = Context()
    module = ModuleType("marimo._runtime.context")
    setattr(module, "safe_get_context", lambda: context)
    monkeypatch.setitem(sys.modules, "marimo._runtime.context", module)
    lifecycle = ModuleType("marimo._runtime.cell_lifecycle_item")
    setattr(lifecycle, "CellLifecycleItem", object)
    monkeypatch.setitem(sys.modules, "marimo._runtime.cell_lifecycle_item", lifecycle)
    transport = ModuleType("marimo._plugins.ui._impl.comm")

    class NativeComm(comm.DummyComm):
        def __init__(self, **kwargs: Any) -> None:
            self._stream = getattr(module, "safe_get_context")().stream
            super().__init__(**kwargs)

    setattr(transport, "MarimoComm", NativeComm)
    monkeypatch.setitem(sys.modules, "marimo._plugins.ui._impl.comm", transport)
    monkeypatch.setattr(comm, "create_comm", NativeComm)
    return context


def test_host_context_restores_callback_owner_and_preserves_executing_cells(
    context: Context,
) -> None:
    host = capture_host()
    context.cell_id = None
    with pytest.raises(RuntimeError, match="callback failed"):
        with host.scope():
            assert context.cell_id == "configure"
            raise RuntimeError("callback failed")
    assert context.cell_id is None
    context.cell_id = "another-cell"
    with host.scope():
        assert context.cell_id == "another-cell"
    assert context.cell_id == "another-cell"


def test_factory_comm_opens_with_wrapped_source_before_private_policy_is_published(
    context: Context,
) -> None:
    class Counter(anywidget.AnyWidget):
        _esm = "export default { render() {} };"
        value = t.Int(0).tag(sync=True)
        notes = t.Unicode("internal").tag(sync=True)

    before = set(widget_module._instances)
    opened: list[tuple[str | None, Any, dict[str, Any]]] = []
    pending_responses: list[dict[str, Any]] = []
    callback = Widget._widget_construction_callback

    def observe_open(widget: Any) -> None:
        if isinstance(widget, Counter):
            opened.append((context.cell_id, widget, widget.get_state()))

    def create_counter() -> Counter:
        widget = Counter()
        assert widget.comm is not None
        with patch.object(
            widget.comm,
            "send",
            side_effect=lambda data, **_: pending_responses.append(data),
        ):
            widget._handle_custom_msg(
                {"kind": "anywidget-webmcp", "id": "pending", "operation": "read"}, []
            )
        return widget

    Widget.on_widget_constructed(observe_open)
    try:
        session = webmcp.enable(
            widgets={create_counter: {"private": {"notes"}}}, discover=False
        )
        context.cell_id = None
        session._handle_custom_msg(
            {
                "kind": "anywidget-webmcp",
                "id": "create",
                "operation": "create",
                "target": session.get_state("_webmcp_catalog")["_webmcp_catalog"][0][
                    "id"
                ],
                "arguments": {},
            },
            [],
        )
        assert len(opened) == 1
        owner, widget, initial = opened[0]
        assert owner == "configure"
        assert context.cell_id is None
        assert initial["_webmcp"] is None
        assert initial["_esm"] != Counter._esm
        assert widget.get_state("_esm")["_esm"] == initial["_esm"]
        assert widget.get_state("_webmcp")["_webmcp"]["properties"] == {
            "value": {"type": "integer"}
        }
        assert pending_responses == []
        session.close()
    finally:
        webmcp.disable()
        Widget.on_widget_constructed(callback)
        for key, widget in list(widget_module._instances.items()):
            if key not in before:
                widget.close()


def test_native_cell_disposal_closes_session_without_publishing_source(
    context: Context,
) -> None:
    class Counter(anywidget.AnyWidget):
        _esm = "export default { render() {} };"

    session = webmcp.enable(widgets=[Counter], discover=False)
    try:
        context.cell_id = None
        session._handle_custom_msg(
            {
                "kind": "anywidget-webmcp",
                "id": "create",
                "operation": "create",
                "target": session.get_state("_webmcp_catalog")["_webmcp_catalog"][0][
                    "id"
                ],
                "arguments": {},
            },
            [],
        )
        reference = session.get_state("_webmcp_created")["_webmcp_created"][0]
        widget = widget_module._instances[reference.removeprefix("anywidget:")]
        disposal_messages: list[dict[str, Any]] = []
        with patch.object(
            widget.comm,
            "send",
            side_effect=lambda data, **_: disposal_messages.append(data),
        ):
            context.cell_lifecycle_registry.dispose(context)
        assert not webmcp.is_enabled()
        assert widget.comm is None
        assert session.comm is None
        assert context.cell_id is None
        assert all("_esm" not in data.get("state", {}) for data in disposal_messages)
    finally:
        session.close()


def test_widget_comm_close_releases_session_and_preserves_borrowed_widgets() -> None:
    class Counter(anywidget.AnyWidget):
        _esm = "export default { render() {} };"

    borrowed = Counter()
    session = webmcp.enable(widgets=[Counter, borrowed], discover=False)
    try:
        session._handle_custom_msg(
            {
                "kind": "anywidget-webmcp",
                "id": "create",
                "operation": "create",
                "target": session.get_state("_webmcp_catalog")["_webmcp_catalog"][0][
                    "id"
                ],
                "arguments": {},
            },
            [],
        )
        reference = session.get_state("_webmcp_created")["_webmcp_created"][0]
        created = widget_module._instances[reference.removeprefix("anywidget:")]
        transport = session.comm
        message = {"content": {"comm_id": session.model_id, "data": {}}}
        unused_stream: Any = None
        comm.get_comm_manager().comm_close(unused_stream, "", message)

        assert not webmcp.is_enabled()
        assert session.comm is None
        assert created.comm is None
        assert borrowed.comm is not None
        assert borrowed.get_state("_webmcp") == {"_webmcp": None}
        assert transport is not None
        transport.handle_close(message)
        session.close()
        assert borrowed.comm is not None
    finally:
        session.close()
        borrowed.close()


def test_native_runtimes_isolate_discovery_configuration_and_lifecycle(
    context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Counter(anywidget.AnyWidget):
        _esm = "export default { render() {} };"
        value = t.Int(0).tag(sync=True)
        notes = t.Unicode("internal").tag(sync=True)

    module = sys.modules["marimo._runtime.context"]
    first_widget = Counter()
    first = webmcp.enable(widgets={Counter: {"private": {"notes"}}})
    second_context = Context()
    second: webmcp.Session | None = None
    second_widget: Counter | None = None
    try:
        monkeypatch.setattr(module, "safe_get_context", lambda: second_context)
        second_widget = Counter()
        second = webmcp.enable(widgets=[Counter])
        assert first is not second
        assert first.get_state("_webmcp_widgets")["_webmcp_widgets"] == [
            f"anywidget:{first_widget.model_id}"
        ]
        assert second.get_state("_webmcp_widgets")["_webmcp_widgets"] == [
            f"anywidget:{second_widget.model_id}"
        ]
        assert set(first_widget.get_state("_webmcp")["_webmcp"]["properties"]) == {
            "value"
        }
        assert set(second_widget.get_state("_webmcp")["_webmcp"]["properties"]) == {
            "value",
            "notes",
        }
        with pytest.raises(ValueError, match="another notebook runtime"):
            webmcp.enable(widgets=[first_widget])

        webmcp.disable()
        assert not webmcp.is_enabled()
        monkeypatch.setattr(module, "safe_get_context", lambda: context)
        context.cell_id = None
        assert webmcp.is_enabled()
        assert webmcp.enable() is first
        second_context.cell_lifecycle_registry.dispose(second_context)
        assert webmcp.is_enabled()
        assert first.comm is not None
        assert second.comm is None
    finally:
        first.close()
        if second is not None:
            second.close()
        first_widget.close()
        if second_widget is not None:
            second_widget.close()


def test_default_widget_runtime_excludes_native_runtime_widgets(
    context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Counter(anywidget.AnyWidget):
        _esm = "export default { render() {} };"

    native_widget = Counter()
    native_session = webmcp.enable()
    module = sys.modules["marimo._runtime.context"]
    monkeypatch.setattr(module, "safe_get_context", lambda: None)
    monkeypatch.setattr(comm, "create_comm", comm.DummyComm)
    ordinary_widget = Counter()
    ordinary_session: webmcp.Session | None = None
    try:
        ordinary_session = webmcp.enable()
        assert ordinary_session.get_state("_webmcp_widgets")["_webmcp_widgets"] == [
            f"anywidget:{ordinary_widget.model_id}"
        ]
        with pytest.raises(ValueError, match="another notebook runtime"):
            webmcp.enable(widgets=[native_widget])
        webmcp.disable()
        monkeypatch.setattr(module, "safe_get_context", lambda: context)
        assert webmcp.is_enabled()
        assert webmcp.enable() is native_session
    finally:
        native_session.close()
        if ordinary_session is not None:
            ordinary_session.close()
        native_widget.close()
        ordinary_widget.close()


def test_factory_rejects_child_from_another_native_runtime(
    context: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Counter(anywidget.AnyWidget):
        _esm = "export default { render() {} };"
        notes = t.Unicode("internal").tag(sync=True)

    class Parent(anywidget.AnyWidget):
        _esm = "export default { render() {} };"
        child = anywidget.WidgetTrait().tag(sync=True)

    borrowed = Counter()
    first = webmcp.enable(widgets={borrowed: {"private": {"notes"}}})
    descriptor = borrowed.get_state("_webmcp")
    second_context = Context()
    module = sys.modules["marimo._runtime.context"]
    monkeypatch.setattr(module, "safe_get_context", lambda: second_context)
    roots: list[Parent] = []

    def create_parent() -> Parent:
        root = Parent(child=borrowed)
        roots.append(root)
        return root

    second = webmcp.enable(widgets=[create_parent], discover=False)
    messages: list[dict[str, Any]] = []
    try:
        assert second.comm is not None
        with patch.object(
            second.comm, "send", side_effect=lambda data, **_: messages.append(data)
        ):
            second._handle_custom_msg(
                {
                    "kind": "anywidget-webmcp",
                    "id": "foreign",
                    "operation": "create",
                    "target": second.get_state("_webmcp_catalog")["_webmcp_catalog"][0][
                        "id"
                    ],
                    "arguments": {},
                },
                [],
            )
        assert "another notebook runtime" in messages[-1]["content"]["error"]
        assert roots[0].comm is None
        assert borrowed.comm is not None
        assert borrowed.get_state("_webmcp") == descriptor
        assert second.get_state("_webmcp_created") == {"_webmcp_created": []}
    finally:
        first.close()
        second.close()
        borrowed.close()
        for root in roots:
            root.close()


def test_native_closed_transport_is_omitted_from_discovery(context: Context) -> None:
    class Counter(anywidget.AnyWidget):
        _esm = "export default { render() {} };"

    widget = Counter()
    assert widget.comm is not None
    widget.comm.close()
    session = webmcp.enable()
    try:
        assert session.get_state("_webmcp_widgets") == {"_webmcp_widgets": []}
        assert widget.get_state().get("_webmcp") is None
    finally:
        session.close()
        widget.close()


def test_native_closed_transport_is_rejected_as_explicit_target(
    context: Context,
) -> None:
    class Counter(anywidget.AnyWidget):
        _esm = "export default { render() {} };"

    widget = Counter()
    assert widget.comm is not None
    widget.comm.close()
    try:
        with pytest.raises(ValueError, match="open widget comm"):
            webmcp.enable(widgets=[widget])
        assert not webmcp.is_enabled()
    finally:
        widget.close()
