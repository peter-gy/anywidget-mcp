from __future__ import annotations

from collections.abc import Iterator
from typing import Any
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
    notes = t.Unicode("internal").tag(sync=True)


@pytest.fixture(autouse=True)
def close_widgets() -> Iterator[None]:
    before = set(widget_module._instances)
    yield
    webmcp.disable()
    for key, widget in list(widget_module._instances.items()):
        if key not in before:
            widget.close()


def read(widget: anywidget.AnyWidget) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    assert widget.comm is not None
    with patch.object(
        widget.comm, "send", side_effect=lambda data, **_: messages.append(data)
    ):
        widget._handle_custom_msg(
            {"kind": "anywidget-webmcp", "id": "read", "operation": "read"}, []
        )
    return messages[-1]["content"]["result"]["state"]


def test_configuration_snapshots_caller_owned_trait_sets() -> None:
    private = {"notes"}
    read_only = {"value"}
    first = Counter()
    session = webmcp.enable(
        widgets={Counter: {"private": private, "read_only": read_only}},
        discover=True,
    )
    private.clear()
    read_only.clear()
    second = Counter()
    assert webmcp.enable() is session
    for widget in (first, second):
        assert read(widget) == {"value": 0}
        assert widget.get_state("_webmcp")["_webmcp"]["writable"] == {}


def test_cyclic_trait_metadata_allows_webmcp_exposure() -> None:
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle

    class CyclicCounter(Counter):
        value = t.Int().tag(sync=True, display=cycle)
        cache = t.Any(default_value=cycle)

    counter = CyclicCounter()
    webmcp.enable(widgets={counter: {"private": {"notes"}}}, discover=False)
    assert read(counter) == {"value": 0}


def test_conflicting_widget_preserves_configured_privacy_and_discovery() -> None:
    class Conflicting(anywidget.AnyWidget):
        _esm = "export default { render() {} };"
        _webmcp = t.Dict().tag(sync=True)

    counter = Counter()
    conflict = Conflicting()
    session = webmcp.enable(widgets={counter: {"private": {"notes"}}}, discover=False)
    identity = counter.get_state("_webmcp")["_webmcp"]["id"]

    with pytest.raises(ValueError, match="reserved"):
        webmcp.enable(widgets={counter: {"private": set()}}, discover=True)

    assert read(counter) == {"value": 0}
    assert counter.get_state("_webmcp")["_webmcp"]["id"] == identity
    assert conflict.get_state("_webmcp") == {"_webmcp": {}}
    assert Counter().get_state().get("_webmcp") is None
    assert webmcp.enable() is session
    assert read(counter) == {"value": 0}


def test_source_serialization_failure_preserves_previous_configuration() -> None:
    counter = Counter()
    broken = Counter()
    webmcp.enable(widgets={counter: {"private": {"notes"}}}, discover=False)

    def serialize(_source: Any, _widget: Any) -> str:
        raise ValueError("source unavailable")

    t.HasTraits.add_traits(
        broken,
        _esm=t.Unicode().tag(sync=True, to_json=serialize),
    )
    with pytest.raises(ValueError, match="source unavailable"):
        webmcp.enable(widgets={counter: {"private": set()}}, discover=True)

    assert read(counter) == {"value": 0}
    assert Counter().get_state().get("_webmcp") is None


def test_delivery_failure_restores_python_policy_and_published_schema() -> None:
    counter = Counter()
    session = webmcp.enable(widgets={counter: {"private": {"notes"}}}, discover=False)
    descriptor = counter.get_state("_webmcp")
    assert counter.comm is not None
    send = counter.comm.send
    failed = False

    def send_once(data: dict[str, Any], **kwargs: Any) -> None:
        nonlocal failed
        if not failed and "_webmcp" in data.get("state", {}):
            failed = True
            raise RuntimeError("delivery failed")
        send(data, **kwargs)

    with patch.object(counter.comm, "send", side_effect=send_once):
        with pytest.raises(RuntimeError, match="delivery failed"):
            webmcp.enable(widgets={counter: {"private": set()}}, discover=True)

    assert counter.get_state("_webmcp") == descriptor
    assert read(counter) == {"value": 0}
    assert Counter().get_state().get("_webmcp") is None
    assert webmcp.enable() is session


def test_failed_delivery_and_rollback_withdraw_python_access() -> None:
    counter = Counter()
    session = webmcp.enable(widgets={counter: {"private": {"notes"}}}, discover=False)
    assert counter.comm is not None
    with patch.object(
        counter.comm, "send", side_effect=RuntimeError("delivery failed")
    ):
        with pytest.raises(ExceptionGroup, match="configure and restore"):
            webmcp.enable(widgets={counter: {"private": set()}}, discover=True)

    assert not webmcp.is_enabled()
    messages: list[dict[str, Any]] = []
    with patch.object(
        counter.comm, "send", side_effect=lambda data, **_: messages.append(data)
    ):
        counter._handle_custom_msg(
            {"kind": "anywidget-webmcp", "id": "read", "operation": "read"}, []
        )
    assert messages == []
    session.close()
